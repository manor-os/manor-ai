"""Child execution receipts stay intact; only the model transcript is bounded."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from packages.core.ai.agentic_loop import _compact_tool_result_for_context, agentic_loop
from packages.core.ai.runtime.skills import RuntimeSkillToolObservations, runtime_format_invoke_skill_result
from packages.core.ai.runtime.tool_error_contract import model_tool_execution_result
from packages.core.ai.tools.media_tools import _jobs_to_payload


def _raw(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _child_payload(reference: str, *, metadata_key: str = "execution") -> dict:
    return {
        "status": "completed",
        "skill": "video-editor",
        "stop_reason": "completed",
        metadata_key: {
            "scope": "skill_invocation",
            "delivery_assessment": "not_assessed",
            "stop_reason": "completed",
            "rounds": 5,
            "tool_observations": {
                "callback_results_seen": 2,
                "items": [
                    {"tool": "generate_file", "reference": reference, "status": "completed"},
                    {"tool": "compose_video_timeline", "reference": f"{reference}-composed", "status": "completed"},
                ],
            },
            "tools_used": ["generate_file", "compose_video_timeline"],
        },
        "content": "Full child output: " + ("narration and captions " * 900),
    }


def test_short_child_result_is_unchanged():
    payload = _child_payload("doc-short")
    payload["content"] = "A short confirmed child result."
    original = _raw(payload)
    assert model_tool_execution_result("invoke_skill", original, max_chars=4000) is None
    assert _compact_tool_result_for_context("invoke_skill", original) == original


def test_large_child_content_keeps_execution_observations_and_structural_omission():
    original = _raw(_child_payload("doc-long"))
    projected_text = _compact_tool_result_for_context("invoke_skill", original)
    projected = json.loads(projected_text)

    assert len(projected_text) <= 4000
    assert projected["status"] == "completed"
    assert projected["stop_reason"] == "completed"
    assert projected["execution"]["scope"] == "skill_invocation"
    assert projected["execution"]["delivery_assessment"] == "not_assessed"
    assert "doc-long" in json.dumps(projected["execution"])
    assert "truncated" in projected["content"] or "omitted" in projected["content"]
    assert projected["_tool_result_truncated"]["original_chars"] == len(original)
    assert "preview" not in projected
    assert "Full child output" in original


def test_large_domain_result_keeps_child_scope_and_result_shape():
    payload = _child_payload("doc-result", metadata_key="skill_execution")
    payload["content"] = "Business result follows."
    payload["result"] = {f"asset_{index}": {"url": f"/files/{index}", "detail": "x" * 150} for index in range(90)}
    original = _raw(payload)
    projected_text = _compact_tool_result_for_context("invoke_skill", original)
    projected = json.loads(projected_text)

    assert len(projected_text) <= 4000
    assert projected["status"] == "completed"
    assert projected["stop_reason"] == "completed"
    assert projected["skill_execution"]["scope"] == "skill_invocation"
    assert "doc-result" in json.dumps(projected["skill_execution"])
    assert isinstance(projected["result"], dict)
    assert projected["result"].get("_omitted_fields", 0) > 0
    assert "preview" not in projected


def test_flat_business_result_keeps_document_reference_ahead_of_large_report():
    payload = {
        "status": "completed",
        "stop_reason": "completed",
        "document_id": "doc-flat-123",
        "document_path": "/workspace/final-report.md",
        "report": [
            {"heading": f"section-{index}", "body": "analysis " * 300}
            for index in range(40)
        ],
        "skill_execution": _child_payload("flat")["execution"],
    }
    original = _raw(payload)
    projected_text = _compact_tool_result_for_context("invoke_skill", original)
    projected = json.loads(projected_text)

    assert len(projected_text) <= 4000
    assert projected["status"] == "completed"
    assert projected["stop_reason"] == "completed"
    assert projected["document_id"] == "doc-flat-123"
    assert projected["document_path"] == "/workspace/final-report.md"
    assert projected["skill_execution"]["scope"] == "skill_invocation"
    assert isinstance(projected["report"], list)
    assert "preview" not in projected


@pytest.mark.asyncio
async def test_four_child_observations_keep_older_limitations_after_bulk_prompts():
    warning = "Native narration omitted; must compose a separate voice track."
    base = "/api/v1/fs/entity/Workspaces/_by_id/workspace/"
    job = SimpleNamespace(
        id="job-video", kind="video", status="completed", model="bytedance/seedance-2.0",
        params={
            "route_warnings": [warning], "audio_policy": "silent_picture_only",
            "omitted_seedance_inputs": ["generate_audio"], "post_production_intent": "narration",
        },
        result_url=base + "sora-model-intro-zh.mp4", file_size=8372108,
        duration_seconds=15,
    )
    observations = RuntimeSkillToolObservations()
    observations.record("generate_file", _raw({
        "kind": "video", "status": "pending", "job_id": "job-video",
        "prompt": "source prompt " * 180, "warnings": [warning],
        "omitted_seedance_inputs": ["generate_audio"],
    }))
    observations.record("wait_media_jobs", _raw({
        "kind": "media_jobs", "status": "completed",
        "jobs": await _jobs_to_payload("entity", [job]),
        "completed_count": 1, "total_count": 1,
    }))
    observations.record("video_edit", _raw({
        "status": "ready", "session_id": "ved-test",
        "project_path": "Videos/sora-model-zh-intro", "contract_valid": False,
        "missing_project_contract": [
            "BRIEF.md", "STORYBOARD.md", "index.html", "package.json", "video.json", "index.motion.json",
        ],
        "authoring_contract": {"required": "detailed instructions " * 400},
    }))
    observations.record("generate_file", _raw({
        "kind": "audio", "status": "completed", "provider": "openrouter",
        "result_url": base + "sora-narration-zh.mp3",
        "audio_url": base + "sora-narration-zh.mp3",
        "fs_path": "Workspaces/_by_id/workspace/sora-narration-zh.mp3",
        "prompt": "narration sentence " * 120, "purpose": "narration",
        "model": "google/gemini-3.1-flash-tts-preview", "voice": "Zephyr",
        "language": "zh-CN", "duration_seconds": 15, "file_size": 485324,
    }))
    original = runtime_format_invoke_skill_result("video-edit", {
        "stop_reason": "completed", "rounds": 4,
        "tools_used": ["generate_file", "wait_media_jobs", "video_edit", "generate_file"],
        "content": "Audio was generated.",
        "tool_observations": observations.snapshot([]),
    })
    projected_text = _compact_tool_result_for_context("invoke_skill", original)
    projected = json.loads(projected_text)
    child_observations = projected["execution"]["tool_observations"]
    visible = json.dumps(child_observations)

    assert len(projected_text) <= 4000
    assert len(child_observations["items"]) == 4
    assert warning in visible
    assert "silent_picture_only" in visible
    assert "generate_audio" in visible
    assert "narration" in visible
    assert "job-video" in visible
    assert all(name in visible for name in ("generate_file", "wait_media_jobs", "video_edit"))


def test_child_projection_recompresses_without_losing_original_identity():
    original = _raw(_child_payload("doc-repeat"))
    first_text = _compact_tool_result_for_context("invoke_skill", original)
    first = json.loads(first_text)
    second_text = _compact_tool_result_for_context("invoke_skill", first_text, max_chars=1000)
    second = json.loads(second_text)

    assert len(second_text) <= 1000
    assert second["status"] == "completed"
    assert second["stop_reason"] == "completed"
    assert second["execution"]["scope"] == "skill_invocation"
    assert second["_tool_result_truncated"]["original_chars"] == len(original)
    assert second["_tool_result_truncated"]["sha256"] == first["_tool_result_truncated"]["sha256"]
    assert "preview" not in second


@pytest.mark.parametrize("budget", [100, 150, 200, 300, 500, 1000])
def test_child_projection_respects_even_small_context_budgets(budget: int):
    original = _raw(_child_payload("doc-budget"))
    projected_text = _compact_tool_result_for_context("invoke_skill", original, max_chars=budget)
    projected = json.loads(projected_text)
    assert len(projected_text) <= budget
    assert projected["status"] == "completed"
    assert projected["stop_reason"] == "completed"
    assert projected["execution"]["scope"] == "skill_invocation"
    assert "preview" not in projected


@pytest.mark.asyncio
async def test_concurrent_child_projections_do_not_mix_references():
    async def project(index: int) -> None:
        await asyncio.sleep(0)
        reference = f"child-ref-{index}"
        result = json.loads(_compact_tool_result_for_context("invoke_skill", _raw(_child_payload(reference))))
        execution = json.dumps(result["execution"])
        assert reference in execution
        assert "preview" not in result

    await asyncio.gather(*(project(index) for index in range(32)))


def test_error_projection_still_precedes_child_success_projection():
    payload = _child_payload("doc-error")
    payload.update({"status": "failed", "error": "provider_unavailable", "message": "provider failed"})
    projected = json.loads(_compact_tool_result_for_context("invoke_skill", _raw(payload)))
    assert projected["contract"] == "tool_error"
    assert projected["status"] == "error"


def _many_call_result():
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    evidence = RuntimeToolEvidence()
    observations = RuntimeSkillToolObservations()
    for index in range(24):
        name = "read_file" if index == 23 else "write_file"
        receipt = {"status": "completed", "path": "/workspace/result.txt",
                   "content": "Observed result " * 500}
        evidence.observe_tool_result(name, f"call-{index:02d}-exact-reference", receipt)
        observations.record(name, receipt)
    summary = "Verified fields and final values. " * 66 + "FINAL OBSERVED VALUE: ready."
    result = runtime_format_invoke_skill_result("document-editor", {
        "stop_reason": "completed", "rounds": 24, "tools_used": ["write_file", "read_file"],
        "content": summary, "tool_observations": observations.snapshot([]),
        "tool_evidence": evidence.checkpoint_state(),
    })
    return result, summary


def test_child_summary_and_recent_evidence_survive_large_observation_metadata():
    original, summary = _many_call_result()
    projected_text = _compact_tool_result_for_context("invoke_skill", original)
    projected = json.loads(projected_text)

    assert len(projected_text) <= 4000
    assert projected["content"] == summary
    visible = {item["call_id"]: item for item in projected["execution"]["evidence"]}
    assert visible["call-23-exact-reference"]["operation"] == "read_file"
    assert visible["call-22-exact-reference"]["operation"] == "write_file"
    assert projected["execution"]["goal_status"] == "unverified"
    assert projected["_tool_result_truncated"]["scope"] == "model_context_only"


def test_flat_child_summary_is_not_displaced_by_redundant_diagnostics():
    original, summary = _many_call_result()
    payload = json.loads(original)
    payload.pop("content")
    payload.update(summary=summary, run_id="child-run", artifacts=[], goal_status="unverified",
                   evidence=[{"description": "Detail " * 200}], pending_action=None,
                   resume_context={}, tabs=[{"id": 123}], control={})
    projected = json.loads(_compact_tool_result_for_context("invoke_skill", _raw(payload)))
    assert projected["summary"] == summary
    assert any(item["call_id"] == "call-23-exact-reference"
               for item in projected["execution"]["evidence"])


def test_latest_failure_is_not_displaced_by_its_long_error_message():
    original, _ = _many_call_result()
    payload = json.loads(original)
    payload["content"] = "Summary detail " * 174
    payload["execution"]["evidence"][-1].update(
        result="error", error="read_failed", message="Provider detail " * 40,
    )
    projected_text = _compact_tool_result_for_context("invoke_skill", _raw(payload))
    projected = json.loads(projected_text)
    latest = projected["execution"]["evidence"][-1]
    assert len(projected_text) <= 4000
    assert latest["call_id"] == "call-23-exact-reference"
    assert latest["result"] == "error"


def test_long_summary_does_not_turn_an_artifact_path_into_a_partial_path():
    original, _ = _many_call_result()
    payload = json.loads(original)
    payload.update(content="Summary detail " * 174, document_id="doc-result",
                   document_path="/workspace/" + "/".join(["long-directory"] * 20) + "/report.md")
    projected_text = _compact_tool_result_for_context("invoke_skill", _raw(payload))
    projected = json.loads(projected_text)
    assert len(projected_text) <= 4000
    assert projected["document_id"] == payload["document_id"]
    assert projected["document_path"] == payload["document_path"]


def test_oversized_artifact_identity_is_omitted_whole():
    original, _ = _many_call_result()
    payload = json.loads(original)
    payload["document_url"] = "https://example.test/" + "long-path/" * 500
    projected = json.loads(_compact_tool_result_for_context("invoke_skill", _raw(payload)))
    assert "document_url" not in projected
    assert "document_url" in projected["_omitted_identity_fields"]


@pytest.mark.parametrize("budget", [1000, 2000, 4000])
def test_child_evidence_references_are_atomic_under_context_pressure(budget):
    original, _ = _many_call_result()
    projected = json.loads(_compact_tool_result_for_context("invoke_skill", original, max_chars=budget))
    raw_facts = {item["call_id"]: item for item in original.execution_evidence}
    for fact in projected["execution"].get("evidence", []):
        assert fact["call_id"] in raw_facts
        for key in ("operation", "stage", "result", "semantic_success_verified"):
            assert fact[key] == raw_facts[fact["call_id"]][key]


@pytest.mark.asyncio
async def test_visible_child_references_can_substantiate_reply_without_reexecution():
    from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
    from packages.core.ai.runtime.final_response import FinalResponseAction

    original, _ = _many_call_result()
    contract = ChatEvidenceContract()
    contract.observe_tool_result("invoke_skill", "parent", original)
    projected = json.loads(_compact_tool_result_for_context("invoke_skill", original))
    final_read = next(item for item in projected["execution"]["evidence"] if item.get("operation") == "read_file")
    reference = "parent/" + final_read["call_id"]
    decision = await contract.validate(_raw({
        "kind": "answer", "text": "The resulting file was read back.",
        "outcome": {"status": "completed", "limitations": [], "evidence_refs": [reference],
                    "claims": [{"operation": "read_file", "status": "completed", "evidence_refs": [reference]}]},
    }))
    assert decision.action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_model_projection_does_not_mutate_tool_callback_receipt():
    original = _raw(_child_payload("receipt-123"))
    seen_raw: list[str] = []
    seen_model_projection: list[dict] = []

    async def model(messages, _tools, **_kwargs):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            return "", [{"id": "child-1", "name": "child_agent", "arguments": {}}], {}
        seen_model_projection.append(json.loads(tool_messages[-1]["content"]))
        return "The child finished its call; final delivery still needs review.", None, {}

    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", AsyncMock(side_effect=model)):
        result = await agentic_loop(
            system_prompt="Inspect child results.",
            user_message="Call the child and report its status.",
            tools=[{"type": "function", "function": {"name": "child_agent", "parameters": {"type": "object"}}}],
            tool_executor=AsyncMock(return_value=original),
            on_tool_end=lambda _name, receipt, *_args: seen_raw.append(receipt),
            max_rounds=3,
        )

    assert result.stop_reason == "completed"
    assert seen_raw == [original]
    assert seen_model_projection[0]["execution"]["scope"] == "skill_invocation"
    # The loop may reserialize a JSON tool receipt before model projection;
    # callback evidence remains the byte-for-byte original above.
    assert seen_model_projection[0]["_tool_result_truncated"]["original_chars"] >= len(original)
