"""StepResult envelope part ③: evidence channel + expects-based completion.

Evidence is mined ONLY from successful tool results in the subagent
transcript (model claims never count), flows worker → complete_lease →
step.evidence_refs, and the plan supervisor mechanically checks it against
each step's declared ``expects`` before a completed plan may stand.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from packages.core.plans.executor import _unmet_expects_issue
from packages.core.plans.schema import Plan
from packages.core.workers.internal import _collect_step_evidence


def _transcript(tool_name: str, payload: dict) -> list[dict]:
    return [
        {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": tool_name}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(payload)},
    ]


# ── collector ──────────────────────────────────────────────────────


def test_successful_publish_tool_result_becomes_publish_evidence():
    evidence = _collect_step_evidence(_transcript(
        "mcp__twitter_x__create_tweet",
        {"tweet_id": "123", "tweet_url": "https://x.com/p/123"},
    ))
    publish = [e for e in evidence if e.get("effect") == "publish"]
    assert len(publish) == 1
    assert publish[0]["kind"] == "tool_effect"
    assert publish[0]["fields"]["tweet_id"] == "123"


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


def test_publish_evidence_satisfies_the_expect():
    issue = _unmet_expects_issue(
        _plan_with_expects("publish_post", ["publish"]),
        [_done_step("publish_post", evidence_refs=[
            {"kind": "tool_effect", "effect": "publish", "fields": {"tweet_id": "1"}},
        ])],
    )
    assert issue is None


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
