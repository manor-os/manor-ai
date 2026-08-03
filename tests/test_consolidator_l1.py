"""M15 L1 — the opt-in LLM summarization layer of the consolidators.

Covers:
* default OFF: seeded failures produce no ``failure_cluster``
  observations, coverage says ``l1: disabled``, L0 output unchanged
* enabled + a well-formed model response: ``failure_cluster``
  observations with the right evidence refs / counts, and the report
  still passes the observation-only contract validator
* enabled + malformed JSON / raising transport: report stays
  ``complete``, coverage ``l1: unavailable``, nothing crashes
* M9.6 privacy: the edit-pattern payload carries field names and counts
  ONLY — no participant ids, no raw human text
* ``compute_input_hash`` changes when the flag flips (cache correctness)
* below the ≥3 threshold no LLM call happens at all
"""
from __future__ import annotations

import asyncio
import json

import pytest

from packages.core.consolidators import REGISTRY, SnapshotContext
from packages.core.consolidators import l1 as l1_layer
from packages.core.consolidators.contract import ConsolidationReportModel
from packages.core.consolidators.registry import compute_input_hash
from packages.core.humans.service import record_contribution
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace
from packages.core.review import begin_review, events_in_window

ENTITY_ID = "01L1CONSENTITY000000000000"

_seq = 0


class _FakeCompletion:
    def __init__(self, content: str):
        self.content = content
        self.usage: dict = {}


def _enable_l1(monkeypatch) -> None:
    monkeypatch.setenv(l1_layer.L1_ENABLED_ENV, "1")
    # A model is "configured" — the transport itself is stubbed per test.
    monkeypatch.setattr(l1_layer, "_model_available", lambda: True)


def _stub_model(monkeypatch, handler) -> list[dict]:
    """Replace the runtime L1 completion; return the captured call list."""
    import packages.core.ai.runtime as runtime

    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return handler(kwargs)

    monkeypatch.setattr(
        runtime, "runtime_execute_consolidator_l1_completion", fake, raising=True,
    )
    return calls


async def _workspace(db) -> Workspace:
    workspace = Workspace(entity_id=ENTITY_ID, name="L1 WS")
    db.add(workspace)
    await db.flush()
    return workspace


async def _failed_task(db, workspace: Workspace, *, service: str, error: str) -> Task:
    """A failed task with a failed step carrying an unstructured error."""
    global _seq
    _seq += 1
    task = Task(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        title=f"Task {_seq}", status="failed", owner_service_key=service,
    )
    db.add(task)
    await db.flush()
    plan = ExecutionPlan(
        entity_id=ENTITY_ID, workspace_id=workspace.id, task_id=task.id,
        plan_dag={}, status="failed",
    )
    db.add(plan)
    await db.flush()
    db.add(ExecutionStep(
        entity_id=ENTITY_ID, workspace_id=workspace.id, plan_id=plan.id,
        step_key=f"step_{_seq}", kind="action", step_status="failed",
        params={}, error={"type": "AdapterError", "message": error},
    ))
    await db.flush()
    await record_event(
        db,
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        event_type=et.EXECUTION_FAILED, source_kind="task", source_id=task.id,
        status="failed", idempotency_key=f"l1-test:{task.id}:failed",
    )
    await asyncio.sleep(0.002)  # ULID ordering across distinct milliseconds
    return task


async def _ctx(db, review) -> SnapshotContext:
    workspace = await db.get(Workspace, review.workspace_id)
    return SnapshotContext(
        review=review, workspace=workspace, events=await events_in_window(db, review),
    )


async def _seed_failures(db, workspace: Workspace, count: int = 3) -> list[Task]:
    return [
        await _failed_task(
            db, workspace,
            service="content_creator",
            error=f"upstream timeout after 30s (attempt {index})",
        )
        for index in range(count)
    ]


# ── gate: OFF by default ───────────────────────────────────────────────

def test_l1_disabled_unless_env_truthy(monkeypatch):
    monkeypatch.delenv(l1_layer.L1_ENABLED_ENV, raising=False)
    assert l1_layer.l1_enabled() is False
    for value in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv(l1_layer.L1_ENABLED_ENV, value)
        assert l1_layer.l1_enabled() is False, value
    for value in ("1", "true", "TRUE", " True "):
        monkeypatch.setenv(l1_layer.L1_ENABLED_ENV, value)
        assert l1_layer.l1_enabled() is True, value


async def test_l1_disabled_leaves_l0_output_unchanged(db_session, monkeypatch):
    monkeypatch.delenv(l1_layer.L1_ENABLED_ENV, raising=False)
    workspace = await _workspace(db_session)
    await _seed_failures(db_session, workspace, count=4)

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    report = await REGISTRY["execution"].run(db_session, await _ctx(db_session, review))

    assert report.status == "complete"
    assert report.analyzer_version == "execution-consolidator-v2"
    assert report.coverage.sources["l1"] == "disabled"
    assert [o for o in report.observations if o.type == "failure_cluster"] == []
    # Deterministic L0 output is untouched by the L1 wiring.
    repeated = [o for o in report.observations if o.type == "repeated_failure_pattern"]
    assert len(repeated) == 1
    assert len(repeated[0].evidence_refs) == 4
    assert report.metrics["failed_total"] == 4


# ── enabled: valid model output → observations ─────────────────────────

async def test_l1_enabled_adds_failure_cluster_observations(db_session, monkeypatch):
    workspace = await _workspace(db_session)
    tasks = await _seed_failures(db_session, workspace, count=3)
    _enable_l1(monkeypatch)
    calls = _stub_model(monkeypatch, lambda _kwargs: _FakeCompletion(json.dumps([
        {"cluster": "upstream API timeout", "count": 3, "member_indexes": [0, 1, 2]},
        # A one-member cluster: below FAILURE_CLUSTER_MIN_COUNT, dropped.
        {"cluster": "unclustered noise", "count": 1, "member_indexes": [99]},
    ])))

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    ctx = await _ctx(db_session, review)
    report = await REGISTRY["execution"].run(db_session, ctx)

    # Budget: exactly one LLM call for this domain in this review.
    assert len(calls) == 1
    assert report.status == "complete"
    assert report.coverage.sources["l1"] == "used"

    clusters = [o for o in report.observations if o.type == "failure_cluster"]
    assert len(clusters) == 1
    assert clusters[0].description == "upstream API timeout (3 executions)"
    failed_event_ids = {
        event.id for event in ctx.events
        if event.event_type == et.EXECUTION_FAILED
        and event.source_id in {task.id for task in tasks}
    }
    assert set(clusters[0].evidence_refs) == failed_event_ids

    # Observation-only contract still holds for LLM-produced content.
    revalidated = ConsolidationReportModel(**report.model_dump())
    assert revalidated.coverage.sources["l1"] == "used"


async def test_l1_prompt_forbids_advice_and_ships_only_compact_records(
    db_session, monkeypatch,
):
    workspace = await _workspace(db_session)
    await _seed_failures(db_session, workspace, count=3)
    _enable_l1(monkeypatch)
    calls = _stub_model(monkeypatch, lambda _kwargs: _FakeCompletion("[]"))

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    await REGISTRY["execution"].run(db_session, await _ctx(db_session, review))

    payload = calls[0]["payload"]
    assert payload and all(
        set(record) == {"count", "sample_error", "service_key"} for record in payload
    )
    assert all(len(record["sample_error"]) <= 200 for record in payload)

    from packages.core.ai.runtime import RUNTIME_CONSOLIDATOR_L1_SYSTEM_PROMPT

    prompt = RUNTIME_CONSOLIDATOR_L1_SYSTEM_PROMPT.lower()
    assert "must not recommend" in prompt
    assert "propose next steps" in prompt
    assert "json only" in prompt


# ── enabled: model unusable → unavailable, never a crash ───────────────

@pytest.mark.parametrize("mode", ["malformed", "raises", "wrong_shape"])
async def test_l1_bad_model_output_degrades_to_unavailable(
    db_session, monkeypatch, mode,
):
    workspace = await _workspace(db_session)
    await _seed_failures(db_session, workspace, count=3)
    _enable_l1(monkeypatch)

    def handler(_kwargs):
        if mode == "raises":
            raise RuntimeError("provider exploded")
        if mode == "malformed":
            return _FakeCompletion("{not json at all")
        return _FakeCompletion(json.dumps([{"cluster": 42, "count": "many"}]))

    _stub_model(monkeypatch, handler)

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    report = await REGISTRY["execution"].run(db_session, await _ctx(db_session, review))

    assert report.status == "complete"
    assert report.coverage.sources["l1"] == "unavailable"
    assert [o for o in report.observations if o.type == "failure_cluster"] == []
    # L0 observations survive an unusable model.
    assert [o for o in report.observations if o.type == "repeated_failure_pattern"]


async def test_l1_without_configured_model_is_unavailable(db_session, monkeypatch):
    workspace = await _workspace(db_session)
    await _seed_failures(db_session, workspace, count=3)
    monkeypatch.setenv(l1_layer.L1_ENABLED_ENV, "1")
    monkeypatch.setattr(l1_layer, "_model_available", lambda: False)

    def explode(**_kwargs):
        raise AssertionError("no model configured — must not call the LLM")

    import packages.core.ai.runtime as runtime
    monkeypatch.setattr(
        runtime, "runtime_execute_consolidator_l1_completion", explode, raising=True,
    )

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    report = await REGISTRY["execution"].run(db_session, await _ctx(db_session, review))

    assert report.status == "complete"
    assert report.coverage.sources["l1"] == "unavailable"


# ── threshold: below 3 → no LLM call at all ────────────────────────────

async def test_two_failures_never_call_the_model(db_session, monkeypatch):
    workspace = await _workspace(db_session)
    await _seed_failures(db_session, workspace, count=2)
    _enable_l1(monkeypatch)

    async def explode(*_args, **_kwargs):
        raise AssertionError("L1 must not run below the ≥3 threshold")

    monkeypatch.setattr(l1_layer, "summarize_failure_clusters", explode)

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    report = await REGISTRY["execution"].run(db_session, await _ctx(db_session, review))

    assert report.status == "complete"
    assert report.coverage.sources["l1"] == "skipped"


async def test_two_contributions_never_call_the_model(db_session, monkeypatch):
    workspace = await _workspace(db_session)
    for _ in range(2):
        await record_contribution(
            db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
            participant_id=generate_ulid(), kind="edit",
            target_kind="task", target_id=generate_ulid(),
            diff_summary={"title": {"changed": True, "len_delta": 4}},
        )
    await asyncio.sleep(0.002)
    _enable_l1(monkeypatch)

    async def explode(*_args, **_kwargs):
        raise AssertionError("L1 must not run below the ≥3 threshold")

    monkeypatch.setattr(l1_layer, "summarize_edit_patterns", explode)

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    report = await REGISTRY["human_participation"].run(
        db_session, await _ctx(db_session, review),
    )

    assert report.status == "complete"
    assert report.coverage.sources["l1"] == "skipped"


# ── M9.6 privacy boundary ──────────────────────────────────────────────

async def test_edit_pattern_payload_is_field_counts_only(db_session, monkeypatch):
    """Driven by real HumanContribution rows whose diff_summary has values."""
    workspace = await _workspace(db_session)
    participants = [generate_ulid() for _ in range(3)]
    contributions = []
    for index, participant_id in enumerate(participants):
        contributions.append(await record_contribution(
            db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
            participant_id=participant_id, kind="edit",
            target_kind="task", target_id=f"task_{index}",
            diff_summary={
                "title": {"changed": True, "len_delta": 12 + index},
                "description": {"changed": True, "len_delta": -40},
            },
        ))
    await asyncio.sleep(0.002)
    _enable_l1(monkeypatch)

    captured: list[list[dict]] = []

    async def capture(items, **_kwargs):
        captured.append(items)
        return [{"pattern": "title and description rewritten", "count": 3}]

    monkeypatch.setattr(l1_layer, "summarize_edit_patterns", capture)

    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )
    report = await REGISTRY["human_participation"].run(
        db_session, await _ctx(db_session, review),
    )

    assert len(captured) == 1
    payload = captured[0]
    # ONLY field-name/count aggregates cross the boundary.
    assert all(set(item) == {"field", "count"} for item in payload)
    assert {item["field"] for item in payload} == {"title", "description"}
    assert all(isinstance(item["count"], int) for item in payload)
    blob = json.dumps(payload)
    for identifier in participants + [c.id for c in contributions]:
        assert identifier not in blob
    for leaked in ("len_delta", "changed", "task_0", ENTITY_ID, workspace.id):
        assert leaked not in blob

    patterns = [o for o in report.observations if o.type == "repeated_edit_pattern"]
    assert len(patterns) == 1
    assert patterns[0].description == "title and description rewritten (3 contributions)"
    assert set(patterns[0].evidence_refs) == {
        f"human_contribution:{c.id}" for c in contributions
    }
    assert report.coverage.sources["l1"] == "used"
    assert report.analyzer_version == "human_participation-consolidator-v4"
    # Privacy blacklist still enforced on the L1-augmented report.
    ConsolidationReportModel(**report.model_dump())


async def test_summarize_edit_patterns_strips_unexpected_caller_keys(monkeypatch):
    _enable_l1(monkeypatch)
    calls = _stub_model(
        monkeypatch,
        lambda _kwargs: _FakeCompletion(json.dumps([{"pattern": "title edits", "count": 2}])),
    )

    result = await l1_layer.summarize_edit_patterns([
        {"field": "title", "count": 2, "participant_id": "u_1", "comment": "too long"},
    ])

    assert result == [{"pattern": "title edits", "count": 2}]
    assert calls[0]["payload"] == [{"field": "title", "count": 2}]


# ── input_hash follows the flag ────────────────────────────────────────

async def test_input_hash_changes_when_l1_flag_flips(db_session, monkeypatch):
    workspace = await _workspace(db_session)
    review = await begin_review(
        db_session, entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger="scheduled",
    )

    monkeypatch.delenv(l1_layer.L1_ENABLED_ENV, raising=False)
    off = compute_input_hash("execution", "execution-consolidator-v2", review)
    monkeypatch.setenv(l1_layer.L1_ENABLED_ENV, "1")
    on = compute_input_hash("execution", "execution-consolidator-v2", review)

    assert off != on
    monkeypatch.delenv(l1_layer.L1_ENABLED_ENV, raising=False)
    assert compute_input_hash("execution", "execution-consolidator-v2", review) == off


# ── helper-level schema strictness ─────────────────────────────────────

async def test_summarize_failure_clusters_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv(l1_layer.L1_ENABLED_ENV, raising=False)
    assert await l1_layer.summarize_failure_clusters(
        [{"count": 1, "sample_error": "boom", "service_key": "svc"}]
    ) is None
    assert await l1_layer.summarize_edit_patterns([{"field": "title", "count": 3}]) is None


@pytest.mark.parametrize("body", [
    '{"cluster": "x"}',                                   # object, not array
    '[{"cluster": "x", "count": 2}]',                     # missing member_indexes
    '[{"cluster": "", "count": 2, "member_indexes": [0]}]',   # empty label
    '[{"cluster": "x", "count": 0, "member_indexes": [0]}]',  # non-positive count
    '[{"cluster": "x", "count": 2, "member_indexes": "0"}]',  # wrong index type
    '["just a string"]',                                  # wrong member type
])
async def test_failure_cluster_schema_is_strict(monkeypatch, body):
    _enable_l1(monkeypatch)
    _stub_model(monkeypatch, lambda _kwargs: _FakeCompletion(body))

    assert await l1_layer.summarize_failure_clusters([
        {"count": 1, "sample_error": "boom", "service_key": "svc"},
    ]) is None


async def test_failure_cluster_accepts_fenced_json(monkeypatch):
    _enable_l1(monkeypatch)
    _stub_model(monkeypatch, lambda _kwargs: _FakeCompletion(
        '```json\n[{"cluster": "timeouts", "count": 2, "member_indexes": [0]}]\n```'
    ))

    assert await l1_layer.summarize_failure_clusters([
        {"count": 2, "sample_error": "boom", "service_key": "svc"},
    ]) == [{"cluster": "timeouts", "count": 2, "member_indexes": [0]}]
