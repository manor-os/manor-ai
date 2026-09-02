"""Regression coverage for references into explicit agent output contracts.

Canonical ``output_shape`` contracts expose their payload fields directly on
``steps.<key>.result``.  The generic legacy ``StepResult`` envelope is the only
agent result that exposes an ``outputs`` wrapper (task-level structured output
also deliberately uses that envelope).  Keeping these cases distinct prevents
the Planner's old ``result.outputs.files`` examples from poisoning otherwise
valid artifact/review DAGs before execution starts.
"""
from __future__ import annotations

import pytest

from packages.core.ai.runtime.planning import runtime_planner_system_prompt
from packages.core.contracts.shapes import get_shape
from packages.core.contracts.task_output import plan_output_contract_schema
from packages.core.contracts.task_output import (
    MissingOutputContract,
    OutputContractKind,
    output_contract_for_schema,
)
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.refs import resolve_refs
from packages.core.plans.service import persisted_plan_contract_gaps, plan_contract_gaps
from packages.core.services.task_retry_service import _persisted_plan_replan_context
from packages.core.workers.submit_result import step_result_from_submit


_DIRECT_ARTIFACT_REF = "${{ steps.prepare_outreach_pack.result.files }}"
_LEGACY_ARTIFACT_REF = "${{ steps.prepare_outreach_pack.result.outputs.files }}"
_LEGACY_ENVELOPE_REF = "${{ steps.prepare_outreach_pack.result.outputs }}"


def _outreach_plan(artifact_ref: str) -> Plan:
    """Build the production-shaped artifact → review → report DAG.

    The human review binds ``review_artifacts`` to the upstream files, while
    the report step consumes the same files in its prompt.  This is the shape
    that previously produced the two screenshot errors for
    ``prepare_outreach_pack.outputs``.
    """
    return Plan(
        steps=[
            PlanStep(
                key="prepare_outreach_pack",
                kind="subagent",
                service_key="partnership_development",
                output_shape="ArtifactResult",
                params={"prompt": "Prepare the outreach packet as workspace files."},
            ),
            PlanStep(
                key="calvin_approval",
                kind="human",
                params={
                    "prompt": "Review the outreach packet and approve it.",
                    "review_title": "Outreach packet review",
                    "review_artifacts": artifact_ref,
                },
                depends_on=["prepare_outreach_pack"],
            ),
            PlanStep(
                key="send_update_and_report",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={
                    "prompt": (
                        "Summarize the approved outreach packet and next steps: "
                        + artifact_ref
                    ),
                },
                depends_on=["calvin_approval"],
            ),
        ]
    )


def test_explicit_artifact_result_rejects_legacy_outputs_reference() -> None:
    """The old envelope path must fail closed for an ArtifactResult producer."""
    gaps = plan_contract_gaps(
        _outreach_plan(_LEGACY_ARTIFACT_REF).topo_order(),
        require_explicit_agent_outputs=True,
    )

    reference_gaps = [
        gap
        for gap in gaps
        if gap.kind == "dangling_reference"
        and gap.step_key in {"calvin_approval", "send_update_and_report"}
    ]
    assert {gap.step_key for gap in reference_gaps} == {
        "calvin_approval",
        "send_update_and_report",
    }
    assert all("outputs" in gap.detail and "['files']" in gap.detail for gap in reference_gaps)


def test_explicit_artifact_result_rejects_bare_legacy_envelope_reference() -> None:
    """The screenshot's bare ``.result.outputs`` path is invalid as well."""
    gaps = plan_contract_gaps(
        _outreach_plan(_LEGACY_ENVELOPE_REF).topo_order(),
        require_explicit_agent_outputs=True,
    )

    assert {
        gap.step_key
        for gap in gaps
        if gap.kind == "dangling_reference" and "outputs" in gap.detail
    } == {"calvin_approval", "send_update_and_report"}


def test_explicit_artifact_result_direct_reference_keeps_review_dag_contract_clean() -> None:
    """Both review and report consumers must use the bare ArtifactResult field."""
    gaps = plan_contract_gaps(
        _outreach_plan(_DIRECT_ARTIFACT_REF).topo_order(),
        require_explicit_agent_outputs=True,
    )

    assert gaps == []


def test_empty_result_rejects_named_field_references() -> None:
    """The canonical empty payload cannot satisfy any named field ref."""
    plan = Plan(
        steps=[
            PlanStep(
                key="run_check",
                kind="subagent",
                service_key="partnership_development",
                output_shape="EmptyResult",
                params={"prompt": "Run the check without a payload."},
            ),
            PlanStep(
                key="summarize_check",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={
                    "prompt": "Summarize ${{ steps.run_check.result.status }}",
                },
                depends_on=["run_check"],
            ),
        ]
    )

    gaps = plan_contract_gaps(
        plan.topo_order(),
        require_explicit_agent_outputs=True,
    )

    assert any(
        gap.kind == "dangling_reference" and gap.step_key == "summarize_check"
        for gap in gaps
    )


def test_explicit_artifact_submission_is_bare_payload_for_direct_references() -> None:
    """The worker and resolver must agree that ArtifactResult has no outputs wrapper."""
    schema = plan_output_contract_schema(get_shape("ArtifactResult").json_schema())
    files = [{"name": "outreach.md", "fs_path": "Workspaces/demo/outreach.md"}]
    result = step_result_from_submit(
        {"summary": "Prepared the packet.", "result": {"files": files}},
        schema,
    )

    assert result == {"files": files}
    assert resolve_refs(
        _DIRECT_ARTIFACT_REF,
        {"prepare_outreach_pack": result},
    ) == files


def test_output_contract_factory_is_the_single_representation_classifier() -> None:
    artifact = plan_output_contract_schema(get_shape("ArtifactResult").json_schema())
    task = {
        "$id": "manor:step-result-envelope/v1",
        "x-manor-contract-source": "task.expected_output",
        "type": "object",
    }

    assert output_contract_for_schema(artifact).kind is OutputContractKind.PLAN_CANONICAL
    assert output_contract_for_schema(task).kind is OutputContractKind.TASK_ENVELOPE
    assert output_contract_for_schema(None).kind is OutputContractKind.NONE


def test_hard_submit_omission_fails_but_explicit_null_reaches_schema_validation() -> None:
    schema = plan_output_contract_schema({"type": "null"})

    with pytest.raises(MissingOutputContract):
        step_result_from_submit({"summary": "missing result"}, schema)

    assert step_result_from_submit(
        {"summary": "explicit null", "result": None}, schema,
    ) is None


def test_strict_planning_rejects_an_empty_custom_schema() -> None:
    plan = Plan(
        steps=[
            PlanStep(
                key="unconstrained",
                kind="subagent",
                service_key="partnership_development",
                expected_output_schema={},
                params={"prompt": "Return anything."},
            )
        ]
    )
    gaps = plan_contract_gaps(plan.topo_order(), require_explicit_agent_outputs=True)
    assert any(gap.kind == "empty_output_contract" for gap in gaps)


def test_explicit_custom_schema_links_declared_payload_fields_directly() -> None:
    """Custom hard contracts must participate in the same direct-ref rule."""
    packet_schema = {
        "type": "object",
        "required": ["packet"],
        "properties": {"packet": {"type": "string"}},
    }
    plan = Plan(
        steps=[
            PlanStep(
                key="write_packet",
                kind="subagent",
                service_key="partnership_development",
                expected_output_schema=packet_schema,
                params={"prompt": "Return the packet field."},
            ),
            PlanStep(
                key="summarize_packet",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={
                    "prompt": "Summarize ${{ steps.write_packet.result.packet }}",
                },
                depends_on=["write_packet"],
            ),
        ]
    )

    assert plan_contract_gaps(
        plan.topo_order(),
        require_explicit_agent_outputs=True,
    ) == []


def test_explicit_custom_schema_rejects_legacy_outputs_wrapper() -> None:
    """A custom bare payload must not silently accept the old envelope path."""
    packet_schema = {
        "type": "object",
        "required": ["packet"],
        "properties": {"packet": {"type": "string"}},
    }
    plan = Plan(
        steps=[
            PlanStep(
                key="write_packet",
                kind="subagent",
                service_key="partnership_development",
                expected_output_schema=packet_schema,
                params={"prompt": "Return the packet field."},
            ),
            PlanStep(
                key="summarize_packet",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={
                    "prompt": "Summarize ${{ steps.write_packet.result.outputs.packet }}",
                },
                depends_on=["write_packet"],
            ),
        ]
    )

    gaps = plan_contract_gaps(
        plan.topo_order(),
        require_explicit_agent_outputs=True,
    )

    assert any(
        gap.kind == "dangling_reference"
        and gap.step_key == "summarize_packet"
        and "outputs" in gap.detail
        for gap in gaps
    )


def test_optional_custom_fields_are_not_treated_as_guaranteed_refs() -> None:
    plan = Plan(
        steps=[
            PlanStep(
                key="write_packet",
                kind="subagent",
                service_key="partnership_development",
                expected_output_schema={
                    "type": "object",
                    "properties": {"packet": {"type": "string"}},
                },
                params={"prompt": "Return packet when available."},
            ),
            PlanStep(
                key="summarize_packet",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={"prompt": "Summarize ${{ steps.write_packet.result.packet }}"},
                depends_on=["write_packet"],
            ),
        ]
    )

    assert any(
        gap.kind == "dangling_reference" and gap.step_key == "summarize_packet"
        for gap in plan_contract_gaps(
            plan.topo_order(), require_explicit_agent_outputs=True,
        )
    )


def test_runtime_receipt_fields_are_not_linked_as_agent_payload_guarantees() -> None:
    plan = Plan(
        steps=[
            PlanStep(
                key="publish",
                kind="subagent",
                service_key="partnership_development",
                expected_output_schema={
                    "type": "object",
                    "required": ["post_text", "post_id"],
                    "properties": {
                        "post_text": {"type": "string"},
                        "post_id": {"type": "string"},
                    },
                },
                params={"prompt": "Publish and return the text."},
            ),
            PlanStep(
                key="report",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={"prompt": "Report id ${{ steps.publish.result.post_id }}"},
                depends_on=["publish"],
            ),
        ]
    )

    assert any(
        gap.kind == "dangling_reference" and gap.step_key == "report"
        for gap in plan_contract_gaps(
            plan.topo_order(), require_explicit_agent_outputs=True,
        )
    )


def test_unresolved_schema_ref_is_a_planning_gap_even_without_consumers() -> None:
    plan = Plan(
        steps=[
            PlanStep(
                key="write_packet",
                kind="subagent",
                service_key="partnership_development",
                expected_output_schema={"$ref": "#/$defs/Missing"},
                params={"prompt": "Return the packet."},
            ),
        ]
    )

    assert any(
        gap.kind == "invalid_output_contract" and gap.step_key == "write_packet"
        for gap in plan_contract_gaps(
            plan.topo_order(), require_explicit_agent_outputs=True,
        )
    )

    # Retry/migration lint intentionally uses the non-strict agent mode, but
    # schema validity remains a hard persistence invariant in that mode too.
    assert any(
        gap.kind == "invalid_output_contract" and gap.step_key == "write_packet"
        for gap in persisted_plan_contract_gaps(plan.model_dump(mode="json"))
    )


def test_unresolved_task_dynamic_ref_uses_the_same_schema_gate() -> None:
    plan = Plan(
        steps=[
            PlanStep(
                key="deliver",
                kind="llm",
                service_key="partnership_development",
                params={"prompt": "Return the final task output."},
            ),
        ]
    )

    gaps = plan_contract_gaps(
        plan.topo_order(),
        task_expected_output={"$dynamicRef": "#Missing"},
        require_explicit_agent_outputs=True,
    )

    assert any(gap.kind == "invalid_task_output_contract" for gap in gaps)


def test_retry_detects_stale_explicit_dag_but_keeps_legacy_envelope_dag() -> None:
    from types import SimpleNamespace

    stale = SimpleNamespace(
        id="old-plan",
        plan_dag=_outreach_plan(_LEGACY_ARTIFACT_REF).model_dump(mode="json"),
    )
    assert _persisted_plan_replan_context(stale) is not None

    legacy = SimpleNamespace(
        id="legacy-plan",
        plan_dag=Plan(
            steps=[
                PlanStep(
                    key="prepare",
                    kind="subagent",
                    service_key="partnership_development",
                    params={"prompt": "Prepare a legacy envelope."},
                ),
                PlanStep(
                    key="review",
                    kind="human",
                    params={
                        "prompt": "Review it.",
                        "review_artifacts": "${{ steps.prepare.result.outputs.files }}",
                    },
                    depends_on=["prepare"],
                ),
            ]
        ).model_dump(mode="json"),
    )
    assert _persisted_plan_replan_context(legacy) is None


def test_retry_detects_artifact_write_conflict_with_binding_constraints() -> None:
    from types import SimpleNamespace

    conflict = SimpleNamespace(
        id="artifact-plan",
        plan_dag=Plan(
            steps=[
                PlanStep(
                    key="materialize",
                    kind="subagent",
                    service_key="knowledge",
                    params={"prompt": "Save the closeout artifact."},
                    output_shape="ArtifactResult",
                ),
            ],
        ).model_dump(mode="json"),
    )

    context = _persisted_plan_replan_context(
        conflict,
        task_details={
            "runtime_context": {
                "instructions": "Do not create or write files, including artifacts.",
            },
        },
    )

    assert context is not None
    assert context["reason"] == "task_constraint_contract_conflict"
    assert context["error_type"] == "TaskConstraintContractConflict"


def test_persisted_unshaped_agent_is_a_legacy_envelope_not_a_new_inferred_shape() -> None:
    """Migration lint must not reinterpret old direct refs as bare payloads."""
    legacy_direct = Plan(
        steps=[
            PlanStep(
                key="prepare",
                kind="subagent",
                service_key="partnership_development",
                params={"prompt": "Prepare a legacy envelope."},
            ),
            PlanStep(
                key="review",
                kind="human",
                params={
                    "prompt": "Review it.",
                    "review_artifacts": "${{ steps.prepare.result.files }}",
                },
                depends_on=["prepare"],
            ),
        ]
    )

    gaps = persisted_plan_contract_gaps(legacy_direct.model_dump(mode="json"))
    assert any(
        gap.kind == "dangling_reference" and gap.step_key == "review"
        for gap in gaps
    )


def test_empty_declared_schema_is_not_repaired_into_step_result_envelope() -> None:
    """An explicit empty schema stays bare and cannot provide ``outputs``."""
    plan = Plan(
        steps=[
            PlanStep(
                key="write_anything",
                kind="subagent",
                service_key="partnership_development",
                expected_output_schema={},
                params={"prompt": "Return any JSON value."},
            ),
            PlanStep(
                key="summarize_anything",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={
                    "prompt": (
                        "Summarize ${{ steps.write_anything.result.outputs.text }}"
                    ),
                },
                depends_on=["write_anything"],
            ),
        ]
    )

    gaps = plan_contract_gaps(
        plan.topo_order(),
        require_explicit_agent_outputs=True,
    )

    assert any(
        gap.kind == "dangling_reference" and gap.step_key == "summarize_anything"
        for gap in gaps
    )


def test_explicit_composed_schema_fails_closed_for_field_references() -> None:
    """Unresolved schema composition must not make arbitrary refs look valid."""
    schemas = [
        {
            "$ref": "#/$defs/Payload",
            "$defs": {
                "Payload": {
                    "type": "object",
                    "required": ["packet"],
                    "properties": {"packet": {"type": "string"}},
                }
            },
        },
        {
            "allOf": [
                {
                    "type": "object",
                    "required": ["packet"],
                    "properties": {"packet": {"type": "string"}},
                }
            ]
        },
        {"type": "object", "properties": {}, "additionalProperties": False},
    ]

    for schema in schemas:
        plan = Plan(
            steps=[
                PlanStep(
                    key="write_packet",
                    kind="subagent",
                    service_key="partnership_development",
                    expected_output_schema=schema,
                    params={"prompt": "Return the packet field."},
                ),
                PlanStep(
                    key="summarize_packet",
                    kind="llm",
                    service_key="partnership_development",
                    output_shape="TextResult",
                    params={
                        "prompt": "Summarize ${{ steps.write_packet.result.outputs }}",
                    },
                    depends_on=["write_packet"],
                ),
            ]
        )

        gaps = plan_contract_gaps(
            plan.topo_order(),
            require_explicit_agent_outputs=True,
        )

        assert any(
            gap.kind == "dangling_reference" and gap.step_key == "summarize_packet"
            for gap in gaps
        )


def test_explicit_non_object_action_schema_rejects_field_references() -> None:
    """A provider array/string result cannot satisfy an object field ref."""
    schemas_and_fields = [
        ({"type": "array", "items": {"type": "string"}}, "items"),
        ({"type": "string"}, "text"),
        ({"type": "object", "additionalProperties": False}, "value"),
    ]

    for schema, field in schemas_and_fields:
        plan = Plan(
            steps=[
                PlanStep(
                    key="lookup",
                    kind="action",
                    service_key="partnership_development",
                    provider="demo_mcp",
                    action_key="lookup",
                    expected_output_schema=schema,
                    params={"query": "packet"},
                ),
                PlanStep(
                    key="summarize_lookup",
                    kind="llm",
                    service_key="partnership_development",
                    output_shape="TextResult",
                    params={
                        "prompt": f"Summarize ${{{{ steps.lookup.result.{field} }}}}",
                    },
                    depends_on=["lookup"],
                ),
            ]
        )

        gaps = plan_contract_gaps(
            plan.topo_order(),
            require_explicit_agent_outputs=True,
        )

        assert any(
            gap.kind == "dangling_reference" and gap.step_key == "summarize_lookup"
            for gap in gaps
        )


def test_action_schema_wins_over_incidental_shape_metadata() -> None:
    """Linker precedence must match action materialization/validation."""
    plan = Plan(
        steps=[
            PlanStep(
                key="lookup",
                kind="action",
                service_key="partnership_development",
                provider="demo_mcp",
                action_key="lookup",
                output_shape="ArtifactResult",
                expected_output_schema={
                    "type": "object",
                    "required": ["packet"],
                    "properties": {"packet": {"type": "string"}},
                },
                params={"query": "packet"},
            ),
            PlanStep(
                key="summarize_lookup",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={"prompt": "Summarize ${{ steps.lookup.result.packet }}"},
                depends_on=["lookup"],
            ),
        ]
    )

    assert plan_contract_gaps(
        plan.topo_order(), require_explicit_agent_outputs=True,
    ) == []


def test_nested_reference_requires_schema_proof_for_each_path_segment() -> None:
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                # Index refs are only contract-safe when the producer must
                # emit that index; an item schema alone also permits an empty
                # array.
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "path": {"type": "string"},
                    },
                },
            }
        },
    }
    producer = PlanStep(
        key="files",
        kind="subagent",
        service_key="partnership_development",
        expected_output_schema=schema,
        params={"prompt": "Return files."},
    )
    consumer = PlanStep(
        key="read_file_name",
        kind="llm",
        service_key="partnership_development",
        output_shape="TextResult",
        params={"prompt": "Read ${{ steps.files.result.files[0].name }}"},
        depends_on=["files"],
    )
    assert plan_contract_gaps(
        Plan(steps=[producer, consumer]).topo_order(),
        require_explicit_agent_outputs=True,
    ) == []

    bad_consumer = consumer.model_copy(
        update={"params": {"prompt": "Read ${{ steps.files.result.files[0].missing }}"}}
    )
    gaps = plan_contract_gaps(
        Plan(steps=[producer, bad_consumer]).topo_order(),
        require_explicit_agent_outputs=True,
    )
    assert any(
        gap.kind == "dangling_reference" and "files[0].missing" in gap.detail
        for gap in gaps
    )

    # The same nested path is not safe when the array may be empty, even if
    # its item schema contains the requested field.
    optional_array_schema = {
        **schema,
        "properties": {
            **schema["properties"],
            "files": {
                **schema["properties"]["files"],
                "minItems": 0,
            },
        },
    }
    optional_producer = producer.model_copy(
        update={"expected_output_schema": optional_array_schema}
    )
    optional_gaps = plan_contract_gaps(
        Plan(steps=[optional_producer, consumer]).topo_order(),
        require_explicit_agent_outputs=True,
    )
    assert any(
        gap.kind == "dangling_reference" and "files[0].name" in gap.detail
        for gap in optional_gaps
    )


def test_legacy_unshaped_agent_keeps_step_result_outputs_compatibility() -> None:
    """Unshaped legacy agents still default to the StepResult envelope."""
    plan = Plan(
        steps=[
            PlanStep(
                key="legacy_draft",
                kind="subagent",
                service_key="partnership_development",
                params={"prompt": "Draft the legacy packet."},
            ),
            PlanStep(
                key="legacy_consumer",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={
                    "prompt": (
                        "Summarize the legacy packet files: "
                        "${{ steps.legacy_draft.result.outputs.files }}"
                    ),
                },
                depends_on=["legacy_draft"],
            ),
        ]
    )

    # No explicit-output enforcement is intentional: this is the migration
    # compatibility path for plans authored before output_shape was required.
    assert plan_contract_gaps(plan.topo_order()) == []


def test_legacy_explicit_step_result_materializes_as_an_envelope() -> None:
    """Persisted legacy StepResult metadata must not become a bare payload."""
    from packages.core.plans.service import _step_from_pydantic

    plan_row = type(
        "_PlanRow",
        (),
        {"id": "plan", "entity_id": "entity", "workspace_id": None},
    )()
    step = PlanStep(
        key="legacy",
        kind="subagent",
        service_key="partnership_development",
        output_shape="StepResult",
        params={"prompt": "Return a legacy envelope."},
    )

    materialized = _step_from_pydantic(
        plan_row,
        step,
        output_shape="StepResult",
    )

    assert (
        output_contract_for_schema(materialized.expected_output_schema).kind
        is OutputContractKind.LEGACY_ENVELOPE
    )


def test_planner_prompt_distinguishes_explicit_payload_and_legacy_envelope_paths() -> None:
    """Prompt examples must not teach the stale universal ``outputs`` path."""
    prompt = runtime_planner_system_prompt(
        subscriptions=[],
        agents_by_id={},
        allowed_service_keys=[],
    )

    # Explicit canonical shapes are bare payloads.  Keep these examples in the
    # prompt so a fresh plan emits refs that the linker and worker agree on.
    assert ".result.files" in prompt
    assert ".result.text" in prompt
    assert "ArtifactResult" in prompt

    # The pre-migration sentence advertised ``outputs.*`` for every producer;
    # retaining that unqualified line would recreate the production failure.
    assert "steps.<key>.result.outputs.text / .outputs.files / .outputs.data / .status." not in prompt

    # ``outputs`` remains valid for the deliberately legacy/task envelope, but
    # the prompt must qualify it rather than present it as canonical payload
    # syntax for explicit output_shape producers.
    assert "StepResult" in prompt
    assert "only for a legacy/unshaped StepResult envelope" in prompt
    assert "${{ steps.<key>.result.outputs.data }}" in prompt
