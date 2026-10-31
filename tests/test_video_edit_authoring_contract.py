"""One factual authoring contract; no runtime repair policy or source rewrites."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from packages.core.ai.tools import video_edit_tools as video


def _valid_project(path: Path) -> dict:
    contract = video._video_authoring_contract()
    example = contract["minimal_example"]
    for name in contract["required_files"]:
        value = example.get(name, "project metadata")
        (path / name).write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return contract


def test_minimal_example_and_machine_schema_match_the_live_static_validator(tmp_path):
    contract = _valid_project(tmp_path)
    schema = contract["motion_assertions"]["schema"]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(contract["minimal_example"]["index.motion.json"])
    schema_kinds = [variant["properties"]["kind"]["const"] for variant in schema["properties"]["assertions"]["items"]["oneOf"]]
    assert schema_kinds == contract["motion_assertions"]["supported_kinds"]
    assert video._validate_project_contract(tmp_path) == []
    feedback = video._video_project_feedback(tmp_path)
    assert feedback["contract_valid"] is True
    assert feedback["contract_violations"] == []
    assert "repair_strategy" not in feedback
    assert "next_step" not in feedback
    assert "approval_required" not in feedback


@pytest.mark.parametrize("source", [
    "./assets/vendor/gsap.min.js", "assets/vendor/gsap.min.js",
    "./assets/../assets/vendor/gsap.min.js", "assets/vendor/gsap.min.js?v=1#local",
])
def test_equivalent_local_gsap_script_imports_are_accepted(tmp_path, source):
    contract = _valid_project(tmp_path)
    html = contract["minimal_example"]["index.html"].replace("./assets/vendor/gsap.min.js", source)
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    assert video._validate_project_contract(tmp_path) == []


@pytest.mark.parametrize("replacement", [
    '<!-- <script src="./assets/vendor/gsap.min.js"></script> -->',
    '<p>./assets/vendor/gsap.min.js</p>',
    '<script src="https://cdn.invalid/assets/vendor/gsap.min.js"></script>',
    '<script src="//cdn.invalid/assets/vendor/gsap.min.js"></script>',
    '<script src="http://[malformed/assets/vendor/gsap.min.js"></script>',
    '<script src="../assets/vendor/gsap.min.js"></script>',
])
def test_gsap_must_be_a_real_local_script_tag_not_text_comment_or_external_path(tmp_path, replacement):
    contract = _valid_project(tmp_path)
    html = contract["minimal_example"]["index.html"].replace(
        '<script src="./assets/vendor/gsap.min.js"></script>', replacement,
    )
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    feedback = video._video_project_feedback(tmp_path)
    issue = next(item for item in feedback["contract_violations"] if item["rule"] == "local_gsap_import")
    assert issue["file"] == "index.html"
    assert issue["expected"] == '<script src="./assets/vendor/gsap.min.js"></script>'
    assert "repair_hint" not in issue
    assert feedback["contract_valid"] is False


@pytest.mark.parametrize("assertion", [
    {"kind": "appearsBy", "selector": "#headline", "bySec": 0},
    {"kind": "before", "a": "#headline", "b": "#scene-1"},
    {"kind": "staysInFrame", "selector": "#headline"},
    {"kind": "keepsMoving", "withinSelector": "#scene-1", "maxStaticSec": 2},
    {"kind": "keepsMoving"},
])
def test_supported_motion_assertion_shapes_match_the_returned_schema(tmp_path, assertion):
    contract = _valid_project(tmp_path)
    motion = {"version": 1, "duration": 4, "assertions": [assertion]}
    (tmp_path / "index.motion.json").write_text(json.dumps(motion), encoding="utf-8")
    Draft202012Validator(contract["motion_assertions"]["schema"]).validate(motion)
    assert video._validate_project_contract(tmp_path) == []


@pytest.mark.parametrize("assertion,pointer", [
    ({"kind": "clip_count", "expected": 3}, "/assertions/0/kind"),
    ({"kind": "track_count", "expected": 2}, "/assertions/0/kind"),
    ({"kind": ["staysInFrame"]}, "/assertions/0/kind"),
    ({"kind": "appearsBy", "selector": "#headline"}, "/assertions/0/"),
    ({"kind": "appearsBy", "selector": "#headline", "bySec": -1}, "/assertions/0/bySec"),
    ({"kind": "before", "a": "", "b": "#headline"}, "/assertions/0/a"),
    ({"kind": "keepsMoving", "withinSelector": "*"}, "/assertions/0/withinSelector"),
    ({"kind": "keepsMoving", "maxStaticSec": 0}, "/assertions/0/maxStaticSec"),
    ({"kind": "keepsMoving", "maxStaticSec": float("inf")}, "/assertions/0/maxStaticSec"),
])
def test_invalid_assertions_return_exact_file_rule_and_pointer(tmp_path, assertion, pointer):
    _valid_project(tmp_path)
    (tmp_path / "hook.motion.json").write_text(json.dumps({"assertions": [assertion]}), encoding="utf-8")
    feedback = video._video_project_feedback(tmp_path)
    issue = next(item for item in feedback["contract_violations"] if item["file"] == "hook.motion.json")
    assert issue["pointer"] == pointer
    assert issue["expected"]
    assert issue["message"]
    assert "repair_hint" not in issue
    assert feedback["contract_valid"] is False


@pytest.mark.parametrize("metadata", [{"version": 2}, {"duration": 0}, {"duration": float("nan")}])
def test_motion_metadata_cannot_claim_a_different_or_nonfinite_contract(tmp_path, metadata):
    contract = _valid_project(tmp_path)
    spec = {**contract["minimal_example"]["index.motion.json"], **metadata}
    (tmp_path / "index.motion.json").write_text(json.dumps(spec), encoding="utf-8")
    assert any(item["rule"] == "motion_metadata" for item in video._project_contract_violations(tmp_path))


def test_all_invalid_old_sidecars_are_reported_and_preserved(tmp_path):
    _valid_project(tmp_path)
    originals = {}
    for name in ("hook", "body", "cta", "timeline"):
        content = json.dumps({"tracks": [{"name": name}], "keyframes": [0, 1]})
        originals[f"{name}.motion.json"] = content
        (tmp_path / f"{name}.motion.json").write_text(content, encoding="utf-8")
    feedback = video._video_project_feedback(tmp_path)
    assert {item["file"] for item in feedback["contract_violations"]} == set(originals)
    assert len(feedback["contract_violations"]) == 4
    for issue in feedback["contract_violations"]:
        assert issue["pointer"] == "/assertions"
        assert issue["expected"] == {"type": "array", "minItems": 1}
    for name, content in originals.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == content


def test_unchanged_invalid_sources_report_same_facts_without_choosing_a_repair(tmp_path):
    _valid_project(tmp_path)
    (tmp_path / "index.html").write_text("<main>Missing root clips and import</main>", encoding="utf-8")
    before = video._video_project_feedback(tmp_path)
    (tmp_path / "hook.html").write_text("<section>Edited unrelated scene</section>", encoding="utf-8")
    after = video._video_project_feedback(tmp_path)
    assert after == before
    assert {item["file"] for item in after["contract_violations"]} == {"index.html"}
    assert not {"repair_strategy", "repair_files", "unchanged_contract_rules", "next_step"} & after.keys()
    (tmp_path / "index.html").write_text(after["authoring_contract"]["minimal_example"]["index.html"], encoding="utf-8")
    repaired = video._video_project_feedback(tmp_path)
    assert repaired["contract_valid"] is True
    assert repaired["contract_violations"] == []
    assert (tmp_path / "hook.html").is_file()


@pytest.mark.asyncio
async def test_review_contract_failure_is_a_completed_validation_with_diagnostics(tmp_path, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(video, "_run_video_sandbox", run)
    expected = video._video_project_feedback(tmp_path)
    result = json.loads(await video._review_project(
        project_dir=tmp_path, project_rel="Videos/test", entity_root=tmp_path,
        entity_id="entity", context=None, snapshot_times=None,
    ))
    assert result["status"] == "completed"
    assert result["review_passed"] is False
    assert result["contract_valid"] is False
    assert result["diagnostics"][0]["code"] == "project_contract_incomplete"
    assert result["authoring_contract"] == expected["authoring_contract"]
    assert result["contract_violations"] == expected["contract_violations"]
    assert "review_sha256" not in result
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_static_contract_does_not_bypass_strict_review(tmp_path, monkeypatch):
    _valid_project(tmp_path)
    run = AsyncMock(return_value=(1, {"status": "error", "code": "quality_gate_failed", "stdout": "actual review findings"}, {}, ""))
    monkeypatch.setattr(video, "_run_video_sandbox", run)
    result = json.loads(await video._review_project(
        project_dir=tmp_path, project_rel="Videos/test", entity_root=tmp_path,
        entity_id="entity", context=None, snapshot_times=[0, 2],
    ))
    assert result["status"] == "error"
    assert result["contract"] == "tool_error"
    assert result["error"] == "quality_gate_failed"
    assert result["details"]["contract_valid"] is True
    assert result["details"]["runtime_result"]["stdout"] == "actual review findings"
    assert not {"repair_strategy", "next_step"} & result["details"].keys()
    assert "review_sha256" not in result
    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("review_state,expected_code", [
    ("no_hash", "approval_required"),
    ("not_reviewed", "review_required"),
    ("source_edited", "source_changed_after_review"),
])
async def test_contract_feedback_does_not_bypass_review_or_exact_source_hash(tmp_path, monkeypatch, review_state, expected_code):
    _valid_project(tmp_path)
    original_hash = video._project_source_sha256(tmp_path)
    state = {"last_review_sha256": original_hash}
    if review_state == "not_reviewed":
        state["last_review_sha256"] = ""
    elif review_state == "source_edited":
        index = tmp_path / "index.html"
        index.write_text(index.read_text(encoding="utf-8").replace("Your video content", "Updated content"), encoding="utf-8")
    assert video._video_project_feedback(tmp_path)["contract_valid"] is True
    monkeypatch.setattr(video, "_ensure_video_session_sandbox", AsyncMock())
    monkeypatch.setattr(video, "_sync_video_session_to_entity", AsyncMock())
    run = AsyncMock()
    monkeypatch.setattr(video, "_run_video_sandbox", run)
    result = json.loads(await video._render_project(
        project_dir=tmp_path, project_rel="Videos/test", entity_root=tmp_path,
        entity_id="entity", context=None, review_sha256="" if review_state == "no_hash" else original_hash,
        output_name="renders/final.mp4", quality="high", session_state=state,
    ))
    assert result["error"] == expected_code
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_render_resource_limit_reaches_the_agent_as_a_structured_blocker(tmp_path, monkeypatch):
    """CITY PULSE: the agent saw a bare render failure and asked an operator to
    "retrieve extraction subprocess stderr" it had no way to act on."""
    _valid_project(tmp_path)
    runtime_result = {
        "status": "error", "code": "render_resource_limit", "reason": "pid_limit",
        "step": "video_frame_extraction", "retryable": False, "automatic_retry": "exhausted",
        "error": "The render sandbox hit its process/thread (PID) limit during video frame extraction, "
                 "and again after an automatic retry at minimum concurrency.",
        "attempts": [{"reason": "pid_limit"}, {"reason": "pid_limit"}],
    }
    monkeypatch.setattr(video, "_run_video_sandbox", AsyncMock(return_value=(1, runtime_result, {}, "")))
    result = json.loads(await video._render_project(
        project_dir=tmp_path, project_rel="Videos/test", entity_root=tmp_path, entity_id="entity",
        context=None, review_sha256=video._project_source_sha256(tmp_path),
        output_name="renders/final.mp4", quality="high",
    ))
    assert result["error"] == "render_resource_limit"
    assert result["message"] == runtime_result["error"]
    details = result["details"]
    assert (details["reason"], details["step"]) == ("pid_limit", "video_frame_extraction")
    assert details["retryable"] is False and details["automatic_retry"] == "exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("omitted_assets,renders", [(2, 2), (0, 1)])
async def test_missing_audio_source_renders_again_with_every_file_then_reaches_the_agent(
    tmp_path, monkeypatch, omitted_assets, renders,
):
    """01M58KZRHR9WJG0GMFZJEFPTTY: review passed on the whole project, the render
    left out script-addressed narration, and the agent saw a bare render_failed."""
    _valid_project(tmp_path)
    runtime_result = {
        "status": "error", "code": "render_audio_failed", "reason": "source_not_found",
        "reasons": ["source_not_found"], "step": "audio_process", "retryable": False,
        "automatic_retry": "none", "missing_media": ["assets/references/vo-s03.wav"],
        "error": "The render's audio mix failed (source_not_found) before frame capture.",
    }
    run = AsyncMock(side_effect=lambda **_: (
        1, {**runtime_result, "sandbox_metrics": {"omitted_assets": omitted_assets}}, {}, "",
    ))
    monkeypatch.setattr(video, "_run_video_sandbox", run)
    result = json.loads(await video._render_project(
        project_dir=tmp_path, project_rel="Videos/test", entity_root=tmp_path, entity_id="entity",
        context=None, review_sha256=video._project_source_sha256(tmp_path),
        output_name="renders/final.mp4", quality="high",
    ))
    assert run.await_count == renders
    assert [call.kwargs.get("all_sources", False) for call in run.await_args_list] == [False, True][:renders]
    assert result["error"] == "render_audio_failed"
    assert result["message"] == runtime_result["error"]
    details = result["details"]
    assert (details["reason"], details["step"], details["retryable"]) == ("source_not_found", "audio_process", False)
    assert details["missing_media"] == ["assets/references/vo-s03.wav"]
    assert details["automatic_retry"] == ("all_sources" if renders == 2 else "none")


@pytest.mark.asyncio
@pytest.mark.parametrize("code,reason", [
    ("snapshot_timeout", "navigation_timeout"),
    # Staging EP.05 (2026-10-24): frames with black presenter videos passed review.
    ("snapshot_video_frames_missing", "frame_not_extracted"),
])
async def test_review_host_failure_reaches_the_agent_as_retryable_not_a_project_defect(
    tmp_path, monkeypatch, code, reason,
):
    """01M50AHSWQEPZ7YTC4BQH6FDC0: told to "fix every reported issue", the agent
    edited audio preload, dropped snapshot times, then asked an operator to repair."""
    _valid_project(tmp_path)
    runtime_result = {
        "status": "error", "code": code, "reason": reason,
        "step": "snapshot", "retryable": True, "automatic_retry": "exhausted",
        "error": "Review frame capture failed, and again after an automatic retry. "
                 "Run the same operation again later without editing the project.",
        "attempts": [{"reason": reason}, {"reason": reason}],
        "stderr": "x" * 60_000,
    }
    monkeypatch.setattr(video, "_run_video_sandbox", AsyncMock(return_value=(1, runtime_result, {}, "")))
    result = json.loads(await video._review_project(
        project_dir=tmp_path, project_rel="Videos/test", entity_root=tmp_path,
        entity_id="entity", context=None, snapshot_times=[31.6],
    ))
    assert result["error"] == code
    assert result["message"] == runtime_result["error"]
    details = result["details"]
    assert (details["reason"], details["step"]) == (reason, "snapshot")
    assert details["retryable"] is True and details["automatic_retry"] == "exhausted"
    assert details["attempts"] == 2
    assert "review_sha256" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,runtime_result,expected", [
    # The runtime classified it: its cause, next step and diagnostics reach the agent.
    (1, {
        "status": "error", "code": "render_composition_invalid", "reason": "sub_timeline_readiness_timeout",
        "step": "render", "retryable": False, "automatic_retry": "none",
        "unregistered_timelines": ["s01"], "next_step": "Register a timeline in s01.",
        "error": "HyperFrames blocked render frame capture on sub_timeline_readiness_timeout.",
        "diagnostics": {"exit_code": 1, "stage": "render", "error_lines": ["Render blocked"]},
    }, ("render_composition_invalid", "sub_timeline_readiness_timeout", False)),
    # The runner killed the runtime itself: no JSON, only the exec's status and verdict.
    (137, {"sandbox_outcome": {"outcome": "oom_killed", "detail": "killed: out of memory"}},
     ("sandbox_worker_killed", "oom", True)),
    (1, {}, ("render_failed", "no_result", False)),
])
async def test_render_failures_reach_the_agent_with_cause_next_step_and_diagnostics(
    tmp_path, monkeypatch, caplog, exit_code, runtime_result, expected,
):
    """Local eval 2026-10-21: two renders reported only "Video sandbox render failed
    or produced no MP4" (exit 1), so OOM was blamed for a composition defect."""
    _valid_project(tmp_path)
    monkeypatch.setattr(video, "_run_video_sandbox", AsyncMock(return_value=(exit_code, runtime_result, {}, "tail")))
    with caplog.at_level("WARNING", logger=video.logger.name):
        result = json.loads(await video._render_project(
            project_dir=tmp_path, project_rel="Videos/test", entity_root=tmp_path, entity_id="entity",
            context=None, review_sha256=video._project_source_sha256(tmp_path),
            output_name="renders/final.mp4", quality="high",
        ))
    code, reason, retryable = expected
    details = result["details"]
    assert (result["error"], details["reason"], details["retryable"]) == expected
    assert details["next_step"] and details["diagnostics"]["exit_code"] == exit_code
    if runtime_result.get("error"):
        assert result["message"] == runtime_result["error"]
        assert details["unregistered_timelines"] == ["s01"]
    if code == "sandbox_worker_killed":
        assert details["diagnostics"]["signal"] == 9
        assert details["diagnostics"]["sandbox_outcome"] == "oom_killed"
    assert f"code={code} reason={reason}" in caplog.text


def _with_sub_composition(path: Path, *, host_extra: str = "", source: str = "", script: str = "") -> None:
    index = path / "index.html"
    html = index.read_text(encoding="utf-8").replace(
        "</main>",
        f'<section id="host" class="clip" data-composition-id="scene-a" data-composition-src='
        f'"compositions/a.html" data-start="0" data-duration="4" data-track-index="1"{host_extra}></section></main>',
    )
    index.write_text(html, encoding="utf-8")
    (path / "compositions").mkdir()
    (path / "compositions/a.html").write_text(
        f'<template><div data-composition-id="scene-a">A</div>{source}</template>', encoding="utf-8",
    )
    if script:
        (path / "scenes.js").write_text(script, encoding="utf-8")


@pytest.mark.parametrize("kwargs,flagged", [
    ({}, True),
    ({"host_extra": " data-no-timeline"}, False),
    ({"source": "<script>window.__timelines['scene-a']=gsap.timeline({paused:true});</script>"}, False),
    ({"script": "document.querySelectorAll('[data-composition-id]').forEach(e=>{})"}, True),
    ({"script": "for (const id of ids) window.__timelines[id] = build(id);"}, False),
])
def test_review_rejects_a_sub_composition_whose_timeline_the_render_would_wait_for(tmp_path, kwargs, flagged):
    """Review passed both eval projects; their renders captured every frame and then
    refused to export with sub_timeline_readiness_timeout."""
    _valid_project(tmp_path)
    _with_sub_composition(tmp_path, **kwargs)
    feedback = video._video_project_feedback(tmp_path)
    rules = [item["rule"] for item in feedback["contract_violations"]]
    assert rules == (["sub_composition_timeline"] if flagged else [])
    if flagged:
        violation = feedback["contract_violations"][0]
        assert violation["file"] == "index.html"
        assert "compositions/a.html" in violation["message"] and "data-no-timeline" in violation["expected"]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_recreated", [False, True])
async def test_a_render_resumed_after_its_container_parked_does_not_rebuild_the_session(
    tmp_path, monkeypatch, session_recreated,
):
    """Staging 2026-10-19: the render released the authoring session, parked on
    its render container, and the re-executed call rebuilt the session on the
    default pool -- a ticket the render never needs, which expired."""
    _valid_project(tmp_path)
    approved = video._project_source_sha256(tmp_path)
    state = {"sandbox_id": "session-1", "last_review_sha256": approved}
    ensure = AsyncMock()
    release = AsyncMock()
    monkeypatch.setattr(video, "_ensure_video_session_sandbox", ensure)
    monkeypatch.setattr(video, "_sync_video_session_to_entity", AsyncMock())
    monkeypatch.setattr(video, "_release_video_session_sandbox", release)
    monkeypatch.setattr(video, "save_video_edit_session", AsyncMock())
    parked = video.VideoSandboxParked(object())
    run = AsyncMock(side_effect=[parked, (0, {"status": "ok"}, {}, "")])
    monkeypatch.setattr(video, "_run_video_sandbox", run)

    async def render():
        return await video._render_project(
            project_dir=tmp_path, project_rel="Videos/test", entity_root=tmp_path,
            entity_id="entity", context=None, review_sha256=approved,
            output_name="renders/final.mp4", quality="high", session_state=state,
        )

    with pytest.raises(video.VideoSandboxParked):
        await render()
    assert (ensure.await_count, release.await_count) == (1, 1)

    if session_recreated:
        state["sandbox_id"] = "session-2"  # an authoring call rebuilt it meanwhile
    await render()

    assert run.await_count == 2
    assert ensure.await_count == (2 if session_recreated else 1)
    assert release.await_count == (2 if session_recreated else 1)
