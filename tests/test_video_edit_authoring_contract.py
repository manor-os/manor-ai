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
