"""StepResult envelope part ③: evidence channel + expects-based completion.

Evidence is mined ONLY from successful tool results in the subagent
transcript (model claims never count), flows worker → complete_lease →
step.evidence_refs, and the plan supervisor mechanically checks it against
each step's declared ``expects`` before a completed plan may stand.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.plans.executor import _unmet_expects_failure, _unmet_expects_issue
from packages.core.plans.schema import Plan
from packages.core.workers.internal import _collect_step_evidence


def _transcript(
    tool_name: str,
    payload: dict,
    *,
    arguments: dict | None = None,
) -> list[dict]:
    return [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "c1",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments or {}),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(payload)},
    ]


# ── collector ──────────────────────────────────────────────────────


def test_successful_publish_tool_result_becomes_publish_evidence():
    evidence = _collect_step_evidence(_transcript(
        "mcp__twitter_x__create_tweet",
        {
            "status": "completed",
            "tweet_id": "123",
            "tweet_url": "https://x.com/p/123",
        },
    ))
    publish = [e for e in evidence if e.get("effect") == "publish"]
    assert len(publish) == 1
    assert publish[0]["kind"] == "tool_effect"
    assert publish[0]["fields"]["tweet_id"] == "123"
    assert publish[0]["fields"]["delivery_confirmed"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {
            "ok": True,
            "status": "pending",
            "tweet_id": "not-delivered-yet",
        },
        {"ok": True, "status": "completed", "id": "generic-request-id"},
        {"ok": True},
    ],
    ids=["pending", "generic_id", "ok_only"],
)
def test_classic_worker_does_not_promote_ambiguous_publish_results(payload):
    evidence = _collect_step_evidence(
        _transcript("mcp__twitter_x__create_tweet", payload)
    )

    assert [e for e in evidence if e.get("effect") == "publish"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "completed",
            "tweet_id": "native-1",
            "tweet_url": "https://x.com/p/native-1",
        },
        {
            "ok": True,
            "status": "completed",
            "postcondition": {
                "schema_version": "publication-receipt/v1",
                "platform": "twitter_x",
                "verification_status": "verified",
                "external_id": "typed-1",
                "published_at": "2026-09-19T00:00:00Z",
                "verified_at": "2026-09-19T00:00:01Z",
                "payload_hash": f"sha256:{'a' * 64}",
                "fallback_used": "none",
                "evidence": [{"kind": "tweet_id", "value": "typed-1"}],
                "attempts": [],
            },
        },
    ],
    ids=["native", "typed_postcondition"],
)
def test_classic_worker_promotes_only_confirmed_publish_receipts(payload):
    evidence = _collect_step_evidence(
        _transcript("mcp__twitter_x__create_tweet", payload)
    )

    publish = [e for e in evidence if e.get("effect") == "publish"]
    assert len(publish) == 1
    assert publish[0]["fields"]["delivery_confirmed"] is True


def test_failed_tool_result_produces_no_evidence():
    evidence = _collect_step_evidence(_transcript(
        "mcp__twitter_x__create_tweet",
        {"ok": False, "error": "rate_limited", "tweet_id": "123"},
    ))
    assert [e for e in evidence if e.get("effect") == "publish"] == []


def test_plain_text_answer_produces_no_evidence():
    assert _collect_step_evidence([
        {"role": "assistant", "content": "I posted the tweet, id 999"},
    ]) == []


_GOAL_WRITE = {
    "goal_id": "01GOALWRITE000000000000001",
    "workspace_id": "01WS0000000000000000000001",
    "value": 92.0,
    "recorded_at": "2026-09-22T15:37:00+00:00",
}


def test_successful_update_goal_value_becomes_goal_measurement_evidence():
    evidence = _collect_step_evidence(_transcript("update_goal_value", _GOAL_WRITE))
    measured = [e for e in evidence if e.get("effect") == "goal_measurement"]
    assert measured == [{
        "kind": "tool_effect",
        "effect": "goal_measurement",
        "fields": {
            "goal_id": _GOAL_WRITE["goal_id"],
            "value": 92.0,
            "recorded_at": _GOAL_WRITE["recorded_at"],
        },
    }]


def test_failed_update_goal_value_produces_no_goal_measurement_evidence():
    assert [e for e in _collect_step_evidence(_transcript(
        "update_goal_value",
        {"error": "goal not found", **_GOAL_WRITE},
    )) if e.get("effect") == "goal_measurement"] == []
    assert [e for e in _collect_step_evidence(_transcript(
        "update_goal_value",
        {"ok": False, **_GOAL_WRITE},
    )) if e.get("effect") == "goal_measurement"] == []


def test_prose_goal_score_claim_produces_no_goal_measurement_evidence():
    evidence = _collect_step_evidence([
        {"role": "assistant", "content": "Rights 92 / Distribution 88; recorded_at now."},
    ])
    assert [e for e in evidence if e.get("effect") == "goal_measurement"] == []


def test_workspace_agent_and_manor_goal_write_payloads_become_evidence():
    for tool_name in ("workspace_agent", "manor"):
        evidence = _collect_step_evidence(_transcript(tool_name, _GOAL_WRITE))
        measured = [e for e in evidence if e.get("effect") == "goal_measurement"]
        assert len(measured) == 1, tool_name
        assert measured[0]["fields"]["goal_id"] == _GOAL_WRITE["goal_id"]
        assert measured[0]["fields"]["value"] == 92.0


def test_coordinator_promotes_typed_child_evidence_without_trusting_model_text():
    delegated = {
        "delegated": True,
        "status": "failed",
        "error": "output_schema_validation_failed",
        "evidence_refs": [
            {
                "kind": "tool_effect",
                "effect": "publish",
                "fields": {"id": "p-1", "delivery_confirmed": True},
            },
        ],
    }
    evidence = _collect_step_evidence([
        {
            "role": "assistant",
            "content": "I did nothing.",
            "tool_calls": [{
                "id": "delegate-1",
                "function": {
                    "name": "delegate_to_service",
                    "arguments": "{}",
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "delegate-1",
            "content": json.dumps(delegated),
        },
    ])

    assert evidence == delegated["evidence_refs"]


def test_text_only_step_cannot_declare_publish_completion():
    from packages.core.plans.service import plan_contract_gaps

    plan = Plan.model_validate({"steps": [{
        "key": "publish_post", "kind": "llm", "service_key": "social",
        "params": {"prompt": "Publish"}, "output_shape": "TextResult", "expects": ["publish"],
    }]})
    gaps = plan_contract_gaps(plan.steps)
    assert any(gap.kind == "unexecutable_publish" for gap in gaps)


def test_recovery_issue_humanizes_step_key_without_rewriting_reference():
    from packages.core.plans.executor import _user_facing_step_issue

    issue = "publish_post 仅产出草稿；see https://example.test/publish_post and draft_publish_post.txt"
    result = _user_facing_step_issue(issue, [SimpleNamespace(step_key="publish_post")])
    assert result == "publish post 仅产出草稿；see https://example.test/publish_post and draft_publish_post.txt"


# ── expects vocabulary persists through the plan DAG ───────────────


def test_expects_round_trips_through_plan_dag():
    plan = Plan.model_validate({
        "steps": [{
            "key": "publish_post",
            "kind": "subagent",
            "service_key": "social",
            "params": {"prompt": "publish it"},
            "expects": ["publish"],
        }],
    })
    dumped = plan.model_dump(mode="json")
    assert dumped["steps"][0]["expects"] == ["publish"]


# ── supervisor gate ────────────────────────────────────────────────


def _plan_with_expects(step_key: str, expects: list[str]):
    return SimpleNamespace(plan_dag={
        "steps": [{"key": step_key, "kind": "subagent", "expects": expects}],
    })


def _done_step(step_key: str, evidence_refs=None, result=None):
    return SimpleNamespace(
        step_key=step_key,
        step_status="done",
        evidence_refs=evidence_refs or [],
        result=result or {"text": "I did it"},
    )


def test_unmet_publish_expect_blocks_completion():
    issue = _unmet_expects_issue(
        _plan_with_expects("publish_post", ["publish"]),
        [_done_step("publish_post")],
    )
    assert issue is not None
    assert "publish_post" in issue
    assert "does not count" in issue


def test_unmet_expectations_keep_external_receipts_distinct_from_file_evidence():
    external = _unmet_expects_failure(
        _plan_with_expects("publish_post", ["publish"]),
        [_done_step("publish_post")],
    )
    artifact = _unmet_expects_failure(
        _plan_with_expects("write_report", ["files"]),
        [_done_step("write_report")],
    )
    assert external is not None and external[0] == "external_publish_receipt_missing"
    assert artifact is not None and artifact[0] == "artifact_evidence_missing"


def test_publish_evidence_satisfies_the_expect():
    issue = _unmet_expects_issue(
        _plan_with_expects("publish_post", ["publish"]),
        [_done_step("publish_post", evidence_refs=[
            {
                "kind": "tool_effect",
                "effect": "publish",
                "fields": {"tweet_id": "1", "delivery_confirmed": True},
            },
        ])],
    )
    assert issue is None


def test_publish_evidence_without_delivery_confirmation_does_not_satisfy_expect():
    issue = _unmet_expects_issue(
        _plan_with_expects("publish_post", ["publish"]),
        [_done_step("publish_post", evidence_refs=[
            {
                "kind": "tool_effect",
                "effect": "publish",
                "fields": {"tweet_id": "1"},
            },
        ])],
    )

    assert issue is not None


def test_model_claim_in_result_does_not_satisfy_publish_expect():
    """The whole point: a result field claiming a URL is NOT evidence."""
    issue = _unmet_expects_issue(
        _plan_with_expects("publish_post", ["publish"]),
        [_done_step("publish_post", result={"post_url": "https://x.com/fake"})],
    )
    assert issue is not None


def test_files_expect_satisfied_by_artifact_evidence():
    issue = _unmet_expects_issue(
        _plan_with_expects("write_report", ["files"]),
        [_done_step("write_report", evidence_refs=[
            {"kind": "artifact", "fs_path": "reports/q3.md"},
        ])],
    )
    assert issue is None


@pytest.mark.parametrize(
    "result",
    [
        {"images": ["https://sources.example/model-claimed.png"]},
        {"artifacts": [{"fs_path": "reports/model-claimed.pdf"}]},
    ],
    ids=["images", "artifacts"],
)
def test_model_claimed_artifact_result_does_not_satisfy_files_expect(result):
    issue = _unmet_expects_issue(
        _plan_with_expects("write_report", ["files"]),
        [_done_step("write_report", result=result)],
    )

    assert issue is not None
    assert "no artifact evidence" in issue


def test_successful_file_tool_receipt_satisfies_files_expect():
    payload = {
        "ok": True,
        "status": "completed",
        "saved": True,
        "fs_path": "reports/q3.md",
    }
    evidence = _collect_step_evidence(
        _transcript(
            "save_sandbox_file",
            payload,
            arguments={"path": "reports/q3.md"},
        )
    )

    assert any(item.get("kind") == "artifact" for item in evidence)
    assert _unmet_expects_issue(
        _plan_with_expects("write_report", ["files"]),
        [_done_step("write_report", evidence_refs=evidence, result=payload)],
    ) is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "matches": [{"document_id": "existing-doc", "title": "Prior report"}],
            "documents": [{"document_id": "existing-doc"}],
        },
        {
            "context": "Contents of an existing Knowledge document",
            "document_id": "existing-doc",
        },
    ],
    ids=["read_only_search", "existing_document"],
)
def test_read_only_document_references_do_not_satisfy_files_expect(payload):
    evidence = _collect_step_evidence(_transcript("workspace_search", payload))

    assert not any(item.get("kind") == "artifact" for item in evidence)
    issue = _unmet_expects_issue(
        _plan_with_expects("write_report", ["files"]),
        [_done_step("write_report", evidence_refs=evidence, result=payload)],
    )
    assert issue is not None
    assert "no artifact evidence" in issue


@pytest.mark.parametrize(
    "payload",
    [
        {
            "ok": True,
            "status": "pending",
            "file_url": "https://files.example/pending.pdf",
        },
        {
            "ok": False,
            "status": "failed",
            "created": True,
            "document_id": "failed-document",
        },
        {
            "ok": True,
            "status": "completed",
            "document_id": "existing-document",
        },
    ],
    ids=["pending_file_url", "failed_document", "read_completed_document"],
)
def test_nonterminal_failed_or_read_only_payloads_are_not_artifact_evidence(payload):
    evidence = _collect_step_evidence(_transcript("workspace_document", payload))

    assert [e for e in evidence if e.get("kind") == "artifact"] == []


@pytest.mark.parametrize(
    ("tool_name", "arguments", "payload"),
    [
        (
            "workspace_search",
            {"query": "campaign images"},
            {
                "status": "completed",
                "images": ["https://sources.example/existing-cover.png"],
            },
        ),
        (
            "manor",
            {"action": "list_workspace_artifacts", "params": {}},
            {
                "workspace_id": "ws-1",
                "count": 1,
                "artifacts": [{
                    "type": "image",
                    "document_id": "existing-document",
                    "fs_path": "images/existing-cover.png",
                }],
            },
        ),
    ],
    ids=["search_images", "workspace_artifact_inventory"],
)
def test_read_only_tool_file_collections_do_not_satisfy_files_expect(
    tool_name,
    arguments,
    payload,
):
    evidence = _collect_step_evidence(
        _transcript(tool_name, payload, arguments=arguments)
    )

    assert [e for e in evidence if e.get("kind") == "artifact"] == []
    issue = _unmet_expects_issue(
        _plan_with_expects("write_report", ["files"]),
        [_done_step("write_report", evidence_refs=evidence, result=payload)],
    )
    assert issue is not None
    assert "no artifact evidence" in issue


@pytest.mark.parametrize(
    ("tool_name", "arguments", "payload"),
    [
        (
            "mcp__twitter_x__get_tweet",
            {"tweet_id": "existing-1"},
            {
                "status": "completed",
                "tweet_id": "existing-1",
                "tweet_url": "https://x.com/i/web/status/existing-1",
            },
        ),
        (
            "mcp__linkedin__get_my_posts",
            {},
            {
                "status": "completed",
                "post_id": "existing-2",
                "post_url": "https://linkedin.example/posts/existing-2",
            },
        ),
    ],
    ids=["get_tweet", "get_posts"],
)
def test_read_only_native_post_receipts_do_not_satisfy_publish_expect(
    tool_name,
    arguments,
    payload,
):
    evidence = _collect_step_evidence(
        _transcript(tool_name, payload, arguments=arguments)
    )

    assert [e for e in evidence if e.get("effect") == "publish"] == []
    assert _unmet_expects_issue(
        _plan_with_expects("publish_post", ["publish"]),
        [_done_step("publish_post", evidence_refs=evidence, result=payload)],
    ) is not None


def test_steps_without_expects_are_untouched():
    issue = _unmet_expects_issue(
        _plan_with_expects("draft", []),
        [_done_step("draft")],
    )
    assert issue is None


def test_legacy_string_evidence_refs_are_tolerated():
    issue = _unmet_expects_issue(
        _plan_with_expects("publish_post", ["publish"]),
        [_done_step("publish_post", evidence_refs=["doc:123"])],
    )
    assert issue is not None  # strings aren't publish evidence, but no crash


def test_action_receipt_must_match_the_planned_account():
    plan = SimpleNamespace(execution_mode="live", plan_dag={"steps": [{
        "key": "publish", "kind": "action", "provider": "twitter_x", "action_key": "create_tweet",
        "integration_id": "approved-account",
    }]})
    receipt = {"kind": "tool_effect", "effect": "action", "fields": {
        "provider": "twitter_x", "action_key": "create_tweet", "integration_id": "other-account",
    }}
    step = _done_step("publish", evidence_refs=[receipt])
    assert _unmet_expects_issue(plan, [step]) is not None
    receipt["fields"]["integration_id"] = "approved-account"
    assert _unmet_expects_issue(plan, [step]) is None
