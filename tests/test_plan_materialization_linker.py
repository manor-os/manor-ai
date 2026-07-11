from packages.core.plans.schema import PlanStep
from packages.core.plans.refs import extract_step_refs
from packages.core.plans.service import (
    PlanContractError,
    _resolve_output_shapes,
    _step_from_pydantic,
    plan_contract_gaps,
)


def _llm_step(key, output_shape=None, params=None):
    p = {"prompt": "do the thing"}
    if params:
        p.update(params)
    return PlanStep(key=key, kind="llm", service_key="content_creation", output_shape=output_shape, params=p)


def test_planstep_accepts_output_shape():
    ps = _llm_step("a", output_shape="ArtifactResult")
    assert ps.output_shape == "ArtifactResult"


def test_extract_step_refs_returns_top_level_field():
    params = {"text": "see ${{ steps.write_docs.result.fs_path }}"}
    assert extract_step_refs(params) == [("write_docs", "fs_path")]


def test_extract_step_refs_strips_index_and_subpath():
    params = {"t": "${{ steps.select.result.selected_topics[0].title }}"}
    assert extract_step_refs(params) == [("select", "selected_topics")]


def test_resolve_output_shapes_infers_from_downstream_reference():
    steps = [
        _llm_step("a"),
        _llm_step("b", output_shape="TextResult", params={"prompt": "use ${{ steps.a.result.fs_path }}"}),
    ]
    shapes = _resolve_output_shapes(steps)
    assert shapes["a"] in ("ArtifactResult", "DocumentResult")


def test_step_from_pydantic_derives_schema_from_shape():
    plan_row = type("_PlanRow", (), {"id": "p", "entity_id": "e", "workspace_id": None})()
    ps = _llm_step("a", output_shape="ArtifactResult")
    step = _step_from_pydantic(plan_row, ps, max_attempts=3, output_shape="ArtifactResult")
    assert step.expected_output_schema is not None
    item = step.expected_output_schema["properties"]["files"]["items"]
    assert "fs_path" in item["required"]


def test_canonical_shape_overrides_noncanonical_handwritten_schema():
    # Free-form (llm/subagent): the Planner's guessed schema (e.g. {text}) must
    # NOT win over the resolved canonical shape — that override was the source of
    # the production OutputSchemaError on `research_and_draft`.
    plan_row = type("_PlanRow", (), {"id": "p", "entity_id": "e", "workspace_id": None})()
    bad = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
    ps = _llm_step("a", output_shape="DraftPack").model_copy(update={"expected_output_schema": bad})
    step = _step_from_pydantic(plan_row, ps, max_attempts=3, output_shape="DraftPack")
    assert "drafts" in step.expected_output_schema["properties"]


def test_explicit_canonical_schema_is_kept():
    plan_row = type("_PlanRow", (), {"id": "p", "entity_id": "e", "workspace_id": None})()
    ps = _llm_step("a", output_shape="ArtifactResult")
    step = _step_from_pydantic(plan_row, ps, max_attempts=3, output_shape="ArtifactResult")
    assert "files" in step.expected_output_schema["properties"]


def test_noncanonical_explicit_schema_kept_for_structured_kind():
    # Structured kinds (action/code/...) keep their hand-written schema — it's a
    # real contract with an external system, not a free-form guess.
    from types import SimpleNamespace

    plan_row = type("_PlanRow", (), {"id": "p", "entity_id": "e", "workspace_id": None})()
    explicit = {"type": "object", "required": ["custom"], "properties": {"custom": {"type": "string"}}}
    ps = SimpleNamespace(
        key="a",
        kind="action",
        service_key="s",
        provider="p",
        action_key="act",
        capability_id="c",
        integration_id=None,
        params={},
        expected_input_schema=None,
        expected_output_schema=explicit,
        depends_on=[],
        risk_level="low",
        requires_approval=False,
        max_attempts=3,
    )
    step = _step_from_pydantic(plan_row, ps, max_attempts=3, output_shape="ArtifactResult")
    assert step.expected_output_schema == explicit


def test_plan_contract_gaps_flags_ref_into_unshaped_producer():
    # Real production case: `consolidate_packages` reads
    # `${{ steps.pkg.result.content }}` but `pkg` has no derivable shape.
    steps = [
        _llm_step("pkg"),  # no output_shape, .content can't be inferred
        _llm_step("consolidate", output_shape="TextResult", params={"prompt": "use ${{ steps.pkg.result.content }}"}),
    ]
    gaps = plan_contract_gaps(steps)
    assert any(g.step_key == "consolidate" and g.kind == "dangling_reference" for g in gaps)


def test_plan_contract_gaps_empty_for_clean_plan():
    steps = [
        _llm_step("a", output_shape="ArtifactResult"),
        _llm_step("b", output_shape="TextResult", params={"prompt": "use ${{ steps.a.result.files }}"}),
    ]
    assert plan_contract_gaps(steps) == []


def test_plan_contract_error_carries_gaps():
    steps = [
        _llm_step("pkg"),
        _llm_step("consolidate", output_shape="TextResult", params={"prompt": "use ${{ steps.pkg.result.content }}"}),
    ]
    gaps = plan_contract_gaps(steps)
    err = PlanContractError(gaps)
    assert err.gaps == gaps
    assert "consolidate" in str(err)
