import json
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from packages.core.services.workflow_service import validate_workflow_steps
from packages.core.blueprints.payload import validate_payload
from packages.core.services.github_skill_installer import _parse_skill_frontmatter
from packages.core.templates import REGISTRY
from packages.core.templates.recipes import product_video_studio as template
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.services.workflow_chat_projection import workflow_progress_steps


BLUEPRINT_PATH = (
    Path(__file__).parents[1]
    / "packages/core/blueprints/configs/solo_company/product-video-studio-v1.json"
)
MARKETPLACE_SKILLS_DIR = (
    Path(__file__).parents[1] / "packages/core/ai/marketplace_skills"
)


def _valid_artifact():
    return {
        "artifact_id": "artifact-1",
        "kind": "video",
        "scene_id": "scene-1",
        "document_id": "document-1",
        "source": "browser_capture",
        "mime_type": "video/mp4",
        "status": "ready",
        "provenance": {"workflow_run_id": "run-1", "scene_id": "scene-1"},
    }


def _valid_request():
    return {
        "product_name": "Acme",
        "start_url": "https://app.example.test",
        "audience": "Operations teams",
        "promotion_goal": "Show the approval flow",
        "video_type": "feature_promotion",
        "must_show": ["Submit a request", "See the approved result"],
        "must_not_show": ["Customer data"],
        "final_cta": "Try the workflow",
        "reference_documents": [],
        "reference_assets": [],
        "output_profile": {
            "aspect_ratio": "16:9",
            "width": 1920,
            "height": 1080,
            "target_duration_seconds": {"min": 60, "max": 120},
            "language": "English",
            "voice_profile": "natural_explainer",
            "subtitle_profile": "clean_bottom",
        },
        "browser_session": "current_paired_chrome_session",
    }


def _valid_effect():
    return {
        "effect_id": "effect-1",
        "scene_id": "scene-1",
        "action": "click_element",
        "precondition": {"url": "/requests/new"},
        "expected_postcondition": {"text": "Submitted"},
        "status": "not_started",
        "evidence": [],
        "attempt_count": 0,
    }


def _valid_scene():
    return {
        "scene_id": "scene-1",
        "target_page": "/requests/new",
        "precondition": {"url": "/requests/new"},
        "ordered_actions": [
            {
                "action": "click_element",
                "arguments": {"element_id": "submit"},
                "side_effect": True,
            }
        ],
        "expected_visual_state": {"text": "Submitted"},
        "canonical_narration": "Submit the request for approval.",
        "required_asset_types": ["recording", "screenshot"],
        "acceptance_evidence": [
            {
                "must_show": "Submit a request",
                "observable": "Submitted state is visible",
                "required_asset_type": "screenshot",
            },
            {
                "must_show": "See the approved result",
                "observable": "Approved result is visible",
                "required_asset_type": "recording",
            },
        ],
        "target_duration_seconds": 8,
        "dependencies": [],
        "recovery": "Reload the new request page",
        "privacy": ["Hide personal data"],
    }


def _valid_quality_result():
    return {
        "verdict": "machine_pass",
        "coverage_complete": True,
        "evidence_complete": True,
        "measured_sync_passed": True,
        "covered_must_show": _valid_request()["must_show"],
        "technical_checks": [
            {"check_id": "probe", "status": "pass", "evidence": {"has_video": True}}
        ],
        "visual_findings": [],
        "coverage": [
            {
                "requirement": "Submit a request",
                "status": "covered",
                "scene_ids": ["scene-1"],
                "evidence_artifact_ids": ["artifact-1"],
            },
            {
                "requirement": "See the approved result",
                "status": "covered",
                "scene_ids": ["scene-1"],
                "evidence_artifact_ids": ["artifact-1"],
            },
        ],
        "scene_revisions": [],
        "operator_checks": ["Watch the final MP4"],
    }


def _valid_plan():
    scene = _valid_scene()
    return {
        "plan_version": 1,
        "product_promise": "Approve work without email chains",
        "canonical_narration": scene["canonical_narration"],
        "scene_ids": ["scene-1"],
        "scenes": [scene],
        "must_show_coverage": [
            {
                "requirement": evidence["must_show"],
                "scene_ids": ["scene-1"],
                "acceptance_evidence": [evidence],
            }
            for evidence in scene["acceptance_evidence"]
        ],
        "must_show_coverage_complete": True,
        "covered_must_show": _valid_request()["must_show"],
        "listed_side_effects": ["Submit a request"],
        "estimated_duration_seconds": 75,
        "output_profile": _valid_request()["output_profile"],
        "privacy_notes": ["Use test data"],
    }


def _two_scene_plan():
    plan = _valid_plan()
    first_scene = deepcopy(plan["scenes"][0])
    first_scene["acceptance_evidence"] = [first_scene["acceptance_evidence"][0]]
    second_scene = deepcopy(plan["scenes"][0])
    second_scene["scene_id"] = "scene-2"
    second_scene["target_page"] = "/requests/approved"
    second_scene["acceptance_evidence"] = [second_scene["acceptance_evidence"][1]]
    plan["scene_ids"] = ["scene-1", "scene-2"]
    plan["scenes"] = [first_scene, second_scene]
    plan["must_show_coverage"][0]["scene_ids"] = ["scene-1"]
    plan["must_show_coverage"][1]["scene_ids"] = ["scene-2"]
    return plan


def _artifact_for_scene(scene_id, artifact_id):
    artifact = _valid_artifact()
    artifact["artifact_id"] = artifact_id
    artifact["document_id"] = f"document-{artifact_id}"
    artifact["scene_id"] = scene_id
    artifact["provenance"]["scene_id"] = scene_id
    return artifact


def _collection_artifact(scene, kind, artifact_id):
    artifact = _artifact_for_scene(scene["scene_id"], artifact_id)
    artifact["kind"] = kind
    artifact["mime_type"] = "video/webm" if kind == "video" else "image/png"
    required_asset_type = "recording" if kind == "video" else "screenshot"
    artifact["provenance"]["acceptance_evidence"] = [
        evidence
        for evidence in scene["acceptance_evidence"]
        if evidence["required_asset_type"] == required_asset_type
    ]
    return artifact


def _valid_collection_result(plan=None):
    plan = deepcopy(plan or _valid_plan())
    segments = []
    artifacts = []
    for scene in plan["scenes"]:
        segment = deepcopy(scene)
        segment.update({"status": "completed", "artifact_ids": [], "blocker": None})
        for asset_type in scene["required_asset_types"]:
            kind = {"recording": "video", "screenshot": "image"}[asset_type]
            artifact_id = f"{scene['scene_id']}-{kind}"
            artifact = _collection_artifact(scene, kind, artifact_id)
            artifacts.append(artifact)
            segment["artifact_ids"].append(artifact_id)
        segments.append(segment)
    return {
        "collection_complete": True,
        "segments": segments,
        "artifacts": artifacts,
        "retry_segment_ids": [],
        "manifest_path": "technical/asset-manifest.json",
        "blocker": None,
    }


def _valid_media_probe():
    return {
        "status": "completed",
        "report": {
            "decodable": True,
            "duration_seconds": 8,
            "has_video": True,
            "has_audio": True,
            "video_stream": {"codec": "h264", "width": 1920, "height": 1080},
            "audio_stream": {"codec": "aac", "sample_rate": 48000},
        },
    }


def _valid_audio_analysis():
    return {
        "status": "completed",
        "verdict": "pass",
        "non_empty": True,
        "findings": [],
    }


def _valid_subtitle_analysis():
    return {
        "status": "completed",
        "verdict": "pass",
        "cue_count": 2,
        "maximum_line_count": 2,
        "style_evidence": {
            "Default": {
                "font_size": 52,
                "outline": 2,
                "shadow": 0,
                "alignment": 2,
                "margin_v": 72,
                "bottom_safe": True,
            }
        },
        "findings": [],
    }


def _valid_frame_evidence():
    return {
        "status": "completed",
        "sample_count": 1,
        "frames": [
            {
                "document_id": "frame-document-1",
                "timestamp_seconds": 4,
                "fs_path": "technical/qa-frames/frame-1.png",
            }
        ],
    }


def _production_artifact(kind, artifact_id):
    mime_type = {
        "audio": "audio/wav",
        "subtitle": "text/x-ass",
        "video": "video/mp4",
    }[kind]
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "document_id": f"document-{artifact_id}",
        "source": "generated" if kind == "audio" else "derived",
        "mime_type": mime_type,
        "status": "ready",
        "provenance": {"workflow_run_id": "run-1"},
    }


def _valid_production_result():
    narration = _production_artifact("audio", "narration-1")
    subtitles = _production_artifact("subtitle", "subtitles-1")
    subtitles["fs_path"] = "production/subtitles.ass"
    final_video = _production_artifact("video", "final-video-1")
    return {
        "status": "ready",
        "timeline": {
            "final_video": final_video,
            "narration": narration,
            "subtitles": subtitles,
            "scene_boundaries": [0, 8],
            "visual_scene_intervals": [
                {
                    "scene_id": "scene-1",
                    "visual_start_seconds": 0,
                    "visual_end_seconds": 8,
                    "narration_start_seconds": 0,
                    "narration_end_seconds": 8,
                }
            ],
            "alignment_metrics": {
                "similarity": 0.96,
                "coverage": 1.0,
                "measured_timestamps": True,
                "transcription_model": "groq/whisper-large-v3",
                "aligned_sentence_indexes": [1],
                "missing_sentence_indexes": [],
                "timing_sources": ["measured_stt_words"],
                "sentence_timestamps": [
                    {
                        "sentence_index": 1,
                        "start": 0.2,
                        "end": 7.8,
                        "timing_source": "measured_stt_words",
                    }
                ],
                "scene_coverage": {
                    "coverage": 1.0,
                    "covered_scene_ids": ["scene-1"],
                    "missing_scene_ids": [],
                    "missing_interval_scene_ids": [],
                    "unmapped_scene_ids": [],
                },
            },
        },
        "artifacts": [narration, subtitles, final_video],
        "deficient_scene_ids": [],
        "retry_segment_ids": [],
        "deficit_evidence": [],
        "required_change": None,
        "blocker": None,
    }


def _retake_required_production_result():
    result = _valid_production_result()
    result["timeline"]["visual_scene_intervals"][0]["visual_end_seconds"] = 6
    result.update(
        {
            "status": "retake_required",
            "deficient_scene_ids": ["scene-1"],
            "retry_segment_ids": ["scene-1"],
            "deficit_evidence": [
                {
                    "scene_id": "scene-1",
                    "required_narration_span_seconds": 8,
                    "available_visual_duration_seconds": 6,
                    "deficit_seconds": 2,
                    "allowed_deficit_seconds": 0.75,
                    "threshold_formula": (
                        "min(0.75s, 10% of required_narration_span_seconds)"
                    ),
                    "exceeds_threshold": True,
                }
            ],
            "required_change": "Retake scene-1 with at least 2 additional seconds.",
            "blocker": None,
        }
    )
    return result


def _blocked_production_result():
    result = _valid_production_result()
    result.update(
        {
            "status": "blocked",
            "timeline": None,
            "artifacts": [],
            "deficient_scene_ids": [],
            "retry_segment_ids": [],
            "deficit_evidence": [],
            "required_change": None,
            "blocker": {
                "blocker_type": "subtitle_alignment_unavailable",
                "observed_problem": {
                    "code": "subtitle_timestamp_capable_stt_required",
                    "message": "The configured transcription route returned no timestamps.",
                },
                "required_change": "Enable a timestamp-capable transcription route and retry production.",
                "preserved_receipts": [],
            },
        }
    )
    return result


def _execute_validator(code, inputs):
    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(compile(code, "workflow_validator.py", "exec"), {"inputs": inputs})
    return json.loads(stdout.getvalue())


def _workflow(slug):
    return next(spec for spec in template.WORKFLOW_SPECS if spec["slug"] == slug)


def _executable_steps(workflow_slug):
    for step in _workflow(workflow_slug)["steps"]:
        if step.get("type") == "stage":
            yield from (step.get("config") or {}).get("operations") or []
        else:
            yield step


def _steps_by_id(workflow_slug):
    return {step["id"]: step for step in _executable_steps(workflow_slug)}


def _step(workflow_slug, step_id):
    return _steps_by_id(workflow_slug)[step_id]


def test_product_video_studio_template_registered_without_compatibility_alias():
    registered = REGISTRY["product_video_studio"]

    assert registered.title == "Product Video Studio"
    assert registered.params_schema["required"] == []
    assert "product_demo_video_studio" not in REGISTRY


@pytest.mark.parametrize(
    "schema",
    [
        template.ARTIFACT_REF_SCHEMA,
        template.BROWSER_EFFECT_SCHEMA,
        template.SCENE_PLAN_SCHEMA,
        template.PRODUCT_VIDEO_REQUEST_SCHEMA,
        template.PRODUCT_VIDEO_PLAN_SCHEMA,
        template.QUALITY_RESULT_SCHEMA,
        template.PRODUCT_VIDEO_PROJECT_SCHEMA,
    ],
)
def test_product_video_contract_schemas_are_valid_and_strict(schema):
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False


def test_scene_plan_schema_requires_exact_executable_fields():
    required = [
        "scene_id",
        "target_page",
        "precondition",
        "ordered_actions",
        "expected_visual_state",
        "canonical_narration",
        "required_asset_types",
        "acceptance_evidence",
        "target_duration_seconds",
        "dependencies",
        "recovery",
        "privacy",
    ]

    assert template.SCENE_PLAN_SCHEMA["required"] == required
    assert set(template.SCENE_PLAN_SCHEMA["properties"]) == set(required)
    assert template.SCENE_PLAN_SCHEMA["properties"]["ordered_actions"]["minItems"] == 1
    assert template.SCENE_PLAN_SCHEMA["properties"]["required_asset_types"] == {
        "type": "array",
        "minItems": 1,
        "uniqueItems": True,
        "items": {"type": "string", "enum": ["recording", "screenshot"]},
    }
    assert template.SCENE_PLAN_SCHEMA["properties"]["acceptance_evidence"][
        "minItems"
    ] == 1

    validator = Draft202012Validator(template.SCENE_PLAN_SCHEMA)
    validator.validate(_valid_scene())
    for field in required:
        missing_field = {key: value for key, value in _valid_scene().items() if key != field}
        with pytest.raises(ValidationError, match=f"'{field}' is a required property"):
            validator.validate(missing_field)


def test_product_video_request_declares_stable_ui_field_order():
    assert template.PRODUCT_VIDEO_REQUEST_SCHEMA["x-ui"]["order"] == [
        "product_name",
        "start_url",
        "audience",
        "video_type",
        "promotion_goal",
        "must_show",
        "must_not_show",
        "final_cta",
        "narration_instructions",
        "subtitle_instructions",
        "production_constraints",
        "failure_policy",
        "output_profile",
        "reference_documents",
        "reference_assets",
        "browser_session",
    ]
    assert template.OUTPUT_PROFILE_SCHEMA["x-ui"]["order"] == [
        "aspect_ratio",
        "width",
        "height",
        "target_duration_seconds",
        "language",
        "voice_profile",
        "subtitle_profile",
    ]


def test_product_video_start_requires_only_identity_url_and_required_moments():
    assert template.PRODUCT_VIDEO_REQUEST_SCHEMA["required"] == [
        "product_name",
        "start_url",
        "must_show",
    ]
    assert template.DEFAULT_PRODUCT_VIDEO_REQUEST == {
        "audience": "Prospective and first-time product users",
        "video_type": "feature_promotion",
        "promotion_goal": "Demonstrate the product's key workflow and practical value.",
        "must_show": [],
        "must_not_show": [
            "Credentials, secrets, personal data, and unrelated browser content",
        ],
        "final_cta": (
            "End on the final required product moment; do not add an external publishing CTA."
        ),
        "narration_instructions": (
            "Follow the brief's language and tone and use one continuous narration track."
        ),
        "subtitle_instructions": (
            "Use 52px subtitles on a 1920x1080 canvas, scale proportionally for other "
            "resolutions, keep at most two lines, a light outline, and the bottom safe area."
        ),
        "production_constraints": [
            "Use the current paired browser session",
            "Use only local or user-authorized data",
            "Store every project asset in one project directory in this Workspace Knowledge",
        ],
        "failure_policy": (
            "Pause at the failed node, report the observed problem and editable input, "
            "then resume from that node after correction."
        ),
        "reference_documents": [],
        "reference_assets": [],
        "output_profile": template.DEFAULT_OUTPUT_PROFILE,
        "browser_session": "current_paired_chrome_session",
    }
    run_inputs = {
        item["key"]: item
        for item in template.PRODUCT_VIDEO_REQUEST_RUN_INPUTS
    }
    assert {
        key for key, item in run_inputs.items() if item["required"]
    } == {"product_name", "start_url", "must_show"}
    assert "minItems" not in template.PRODUCT_VIDEO_REQUEST_SCHEMA["properties"][
        "must_not_show"
    ]


def test_product_video_request_requires_an_http_product_url():
    start_url_schema = template.PRODUCT_VIDEO_REQUEST_SCHEMA["properties"]["start_url"]
    must_show_schema = template.PRODUCT_VIDEO_REQUEST_SCHEMA["properties"]["must_show"]
    must_not_show_schema = template.PRODUCT_VIDEO_REQUEST_SCHEMA["properties"]["must_not_show"]

    assert start_url_schema["format"] == "uri"
    assert start_url_schema["pattern"] == r"^[Hh][Tt][Tt][Pp][Ss]?://"
    assert must_show_schema["minItems"] == 1
    assert "minItems" not in must_not_show_schema

    invalid = {**_valid_request(), "start_url": "not-a-url"}
    errors = list(
        Draft202012Validator(
            template.PRODUCT_VIDEO_REQUEST_SCHEMA,
            format_checker=FormatChecker(),
        ).iter_errors(invalid)
    )

    assert any(list(error.absolute_path) == ["start_url"] for error in errors)

    uppercase_scheme = {**_valid_request(), "start_url": "HTTPS://app.example.test"}
    Draft202012Validator(
        template.PRODUCT_VIDEO_REQUEST_SCHEMA,
        format_checker=FormatChecker(),
    ).validate(uppercase_scheme)


def test_product_video_contracts_accept_a_complete_project():
    request = _valid_request()
    scene = _valid_scene()
    quality = _valid_quality_result()
    plan = _valid_plan()
    project = template.initial_product_video_project(request)
    project.update(
        {
            "discovered_journey": {
                "observations": [{"url": request["start_url"], "labels": ["New request"]}],
                "recommended_steps": ["Open form", "Submit request"],
                "assumptions": [],
                "gaps": [],
                "privacy_risks": [],
                "ready_for_planning": True,
            },
            "plan": plan,
            "approved_plan_version": 1,
            "capture_grant_id": "grant-1",
            "scenes": [{**scene, "status": "completed", "artifact_ids": ["artifact-1"]}],
            "artifacts": [_valid_artifact()],
            "quality_result": quality,
        }
    )

    Draft202012Validator(template.PRODUCT_VIDEO_REQUEST_SCHEMA).validate(request)
    Draft202012Validator(template.SCENE_PLAN_SCHEMA).validate(scene)
    Draft202012Validator(template.PRODUCT_VIDEO_PLAN_SCHEMA).validate(plan)
    Draft202012Validator(template.QUALITY_RESULT_SCHEMA).validate(quality)
    Draft202012Validator(template.PRODUCT_VIDEO_PROJECT_SCHEMA).validate(project)


def test_plan_schema_rejects_omitted_or_empty_must_show_coverage():
    validator = Draft202012Validator(template.PRODUCT_VIDEO_PLAN_SCHEMA)
    valid = _valid_plan()
    validator.validate(valid)

    omitted = deepcopy(valid)
    del omitted["must_show_coverage"]
    with pytest.raises(ValidationError):
        validator.validate(omitted)

    empty = deepcopy(valid)
    empty["must_show_coverage"] = []
    with pytest.raises(ValidationError):
        validator.validate(empty)

    empty_requirements = deepcopy(valid)
    empty_requirements["covered_must_show"] = []
    with pytest.raises(ValidationError):
        validator.validate(empty_requirements)


def test_quality_schema_rejects_empty_or_unproven_machine_pass():
    validator = Draft202012Validator(template.QUALITY_RESULT_SCHEMA)
    valid = _valid_quality_result()
    validator.validate(valid)

    empty = deepcopy(valid)
    empty["technical_checks"] = []
    empty["coverage"] = []
    with pytest.raises(ValidationError):
        validator.validate(empty)

    for field in ("coverage_complete", "evidence_complete", "measured_sync_passed"):
        omitted = deepcopy(valid)
        del omitted[field]
        with pytest.raises(ValidationError):
            validator.validate(omitted)

        false_gate = deepcopy(valid)
        false_gate[field] = False
        with pytest.raises(ValidationError):
            validator.validate(false_gate)

    missing_coverage = deepcopy(valid)
    missing_coverage["coverage"][0]["status"] = "missing"
    with pytest.raises(ValidationError):
        validator.validate(missing_coverage)

    missing_evidence = deepcopy(valid)
    missing_evidence["coverage"][0]["evidence_artifact_ids"] = []
    with pytest.raises(ValidationError):
        validator.validate(missing_evidence)

    empty_requirements = deepcopy(valid)
    empty_requirements["covered_must_show"] = []
    with pytest.raises(ValidationError):
        validator.validate(empty_requirements)


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        (template.ARTIFACT_REF_SCHEMA, {"artifact_id": "not-enough"}),
        (template.BROWSER_EFFECT_SCHEMA, {**_valid_effect(), "status": "maybe"}),
        (template.SCENE_PLAN_SCHEMA, {**_valid_scene(), "scene_id": ""}),
        (template.PRODUCT_VIDEO_REQUEST_SCHEMA, {**_valid_request(), "credentials": {}}),
        (template.QUALITY_RESULT_SCHEMA, {**_valid_quality_result(), "verdict": "accepted"}),
    ],
)
def test_product_video_contracts_reject_malformed_values(schema, value):
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(value)


def test_completed_capture_requires_at_least_one_ready_durable_artifact():
    completed_scene = {
        **_valid_scene(),
        "status": "completed",
        "artifact_ids": ["artifact-1", "artifact-image"],
        "blocker": None,
    }
    image_artifact = {
        **_valid_artifact(),
        "artifact_id": "artifact-image",
        "kind": "image",
        "mime_type": "image/png",
    }
    valid_result = {
        "scene": completed_scene,
        "artifacts": [_valid_artifact(), image_artifact],
        "wait_required": False,
        "wait_seconds": 0,
    }

    Draft202012Validator(template.CAPTURE_RESULT_SCHEMA).validate(valid_result)

    with pytest.raises(ValidationError):
        Draft202012Validator(template.CAPTURE_RESULT_SCHEMA).validate(
            {**valid_result, "artifacts": []}
        )

    blocked_artifact = {**_valid_artifact(), "status": "blocked", "document_id": None}
    with pytest.raises(ValidationError):
        Draft202012Validator(template.CAPTURE_RESULT_SCHEMA).validate(
            {**valid_result, "artifacts": [blocked_artifact]}
        )

    with pytest.raises(ValidationError):
        Draft202012Validator(template.CAPTURE_RESULT_SCHEMA).validate(
            {**valid_result, "artifacts": [_valid_artifact()]}
        )

    with pytest.raises(ValidationError):
        Draft202012Validator(template.CAPTURE_RESULT_SCHEMA).validate(
            {**valid_result, "artifacts": [image_artifact]}
        )


@pytest.mark.parametrize(
    "schema",
    [template.CAPTURE_RESULT_SCHEMA, template.COLLECTION_RESULT_SCHEMA],
)
def test_scene_capture_artifacts_require_a_top_level_scene_id(schema):
    collection = _valid_collection_result()
    if schema is template.CAPTURE_RESULT_SCHEMA:
        value = {
            "scene": collection["segments"][0],
            "artifacts": collection["artifacts"],
            "wait_required": False,
            "wait_seconds": 0,
        }
    else:
        value = collection

    invalid = deepcopy(value)
    invalid["artifacts"][0].pop("scene_id")

    with pytest.raises(ValidationError, match="'scene_id' is a required property"):
        Draft202012Validator(schema).validate(invalid)


def test_ready_browser_capture_requires_a_real_document_id():
    invalid_video = {
        **_valid_artifact(),
        "document_id": None,
        "fs_path": "Knowledge/Recordings/scene.webm",
        "knowledge_path": "Knowledge/Recordings/scene.webm",
    }

    with pytest.raises(ValidationError):
        Draft202012Validator(template.ARTIFACT_REF_SCHEMA).validate(invalid_video)


def test_revision_plan_requires_a_stable_revision_history_key():
    revision_entry = template.REVISION_PLAN_SCHEMA["properties"]["revision_entry"]

    assert "revision_id" in revision_entry["required"]
    assert revision_entry["properties"]["revision_id"]["minLength"] == 1


def test_capture_prompt_never_treats_product_state_reuse_as_asset_reuse():
    capture_step = _step("create-product-video-v1", "collect_assets")
    prompt = capture_step["config"]["input"]

    assert "does not authorize skipping the requested screenshot or recording" in prompt
    assert "existing ready artifact IDs" in prompt


def test_capture_rechecks_async_navigation_before_reporting_access_blocker():
    capture_step = _step("create-product-video-v1", "collect_assets")
    chrome_input = capture_step["config"]["forced_tool_calls"][0]["arguments"]["input"]
    collector_prompt = (
        MARKETPLACE_SKILLS_DIR / "screen-asset-collector" / "SKILL.md"
    ).read_text(encoding="utf-8")
    normalized_collector_prompt = " ".join(collector_prompt.split())

    assert "transient loading" in normalized_collector_prompt
    assert "explicit login, permission, or access-denied UI" in normalized_collector_prompt
    assert "visible nested navigation" in normalized_collector_prompt
    assert "Wait for requested states and re-read them" in chrome_input


def test_capture_does_not_invent_page_motion_for_static_recordings():
    capture_step = _step("create-product-video-v1", "collect_assets")
    chrome_input = capture_step["config"]["forced_tool_calls"][0]["arguments"]["input"]
    collector_prompt = " ".join((
        MARKETPLACE_SKILLS_DIR / "screen-asset-collector" / "SKILL.md"
    ).read_text(encoding="utf-8").split())

    assert "Do not invent scrolling, navigation, or other motion" in collector_prompt
    assert "hold the verified state" in collector_prompt
    assert "Do not invent scrolling or navigation for static recordings" in chrome_input


def test_capture_stops_exploring_and_preserves_tabs_after_acceptance_is_visible():
    capture_step = _step("create-product-video-v1", "collect_assets")
    chrome_input = " ".join(
        capture_step["config"]["forced_tool_calls"][0]["arguments"]["input"].split()
    )
    collector_prompt = " ".join((
        MARKETPLACE_SKILLS_DIR / "screen-asset-collector" / "SKILL.md"
    ).read_text(encoding="utf-8").split())

    assert "stop exploratory reads and clicks immediately" in collector_prompt
    assert "one durable PNG receipt and one durable WebM receipt" in collector_prompt
    assert "Do not call finalize_tabs during a per-scene Workflow capture" in collector_prompt
    assert "stop exploring" in chrome_input
    assert "finalize_tabs only after the final phase" in chrome_input


def test_capture_reviews_the_final_bitmap_against_all_visible_acceptance_criteria():
    capture_step = _step("create-product-video-v1", "collect_assets")
    chrome_input = " ".join(
        capture_step["config"]["forced_tool_calls"][0]["arguments"]["input"].split()
    )
    collector_prompt = " ".join((
        MARKETPLACE_SKILLS_DIR / "screen-asset-collector" / "SKILL.md"
    ).read_text(encoding="utf-8").split())

    assert "simultaneously visible in the final viewport" in collector_prompt
    assert "inspect the actual captured bitmap" in collector_prompt
    assert "failed states, unrelated history, stale operator messages" in collector_prompt
    assert "actual bitmap" in chrome_input
    assert "inspect_selector on every required form field" in collector_prompt
    assert "empty or placeholder-only" in collector_prompt
    assert "inspect required form fields" in chrome_input


def test_capture_preserves_approved_scene_identity_and_asset_requirements():
    capture_step = _step("create-product-video-v1", "collect_assets")
    prompt = capture_step["config"]["input"]

    assert "Preserve every Segment ID and required asset type" in prompt


def test_scene_collector_executes_only_the_canonical_scene_contract():
    capture_step = _step("create-product-video-v1", "collect_assets")
    prompt = capture_step["config"]["input"]
    chrome_input = capture_step["config"]["forced_tool_calls"][0]["arguments"][
        "input"
    ]

    assert "Execute only each scene's approved ordered_actions, in order, on target_page" in prompt
    assert "observe expected_visual_state" in prompt
    assert "return every required_asset_types asset and its acceptance_evidence" in prompt
    assert "target_page, ordered_actions, and expected_visual_state" in chrome_input


def test_scene_collector_rejects_proxy_footage_and_mismatched_reuse():
    capture_step = _step("create-product-video-v1", "collect_assets")
    prompt = capture_step["config"]["input"]
    chrome_input = capture_step["config"]["forced_tool_calls"][0]["arguments"][
        "input"
    ]

    assert "Dashboard, home, generic navigation, and other pages are unrelated proxy footage" in prompt
    assert "same scene_id and required asset type" in prompt
    assert "Never substitute Dashboard, home, or another page" in chrome_input


def test_blocked_capture_pauses_for_chat_correction_without_automatic_recovery():
    steps = _steps_by_id("create-product-video-v1")

    assert steps["collection_valid"]["false_next"] == ["save_capture_handoff"]
    assert "recover_assets" not in steps
    assert "recovery_complete" not in steps
    assert "{{retry_segment_ids}}" in steps["collect_assets"]["config"]["input"]
    assert steps["save_capture_handoff"]["next"] == ["build_capture_handoff"]
    assert steps["build_capture_handoff"]["next"] == ["needs_input"]

    retry_state = steps["save_capture_handoff"]["config"]["patch"]["retry_state"]
    assert retry_state["scene_id"] == "{{collection_validation.blocker.scene_id}}"
    assert retry_state["segment_ids"] == "{{collection_validation.retry_segment_ids}}"
    assert retry_state["observed_state"] == "{{collection_validation.blocker.observed_state}}"
    assert retry_state["observed_problem"] == "{{collection_validation.blocker.observed_state}}"
    assert retry_state["required_change"] == "{{collection_validation.blocker.required_change}}"
    assert retry_state["preserved_receipts"] == (
        "{{collection_validation.blocker.preserved_receipts}}"
    )
    assert retry_state["retry_from_step_id"] == "collect_assets"
    assert retry_state["editable_input_schema"]["required"] == [
        "retry_segment_ids"
    ]

    handoff = steps["build_capture_handoff"]["config"]["set"]["input"]
    assert handoff["retry_from_step_id"] == "collect_assets"
    assert handoff["retry_segment_ids"] == "{{collection_validation.retry_segment_ids}}"

    blocked_appends = steps["save_capture_handoff"]["config"]["list_appends"]
    assert {
        "path": "segments",
        "key": "scene_id",
        "items": "{{collection_validation.validated_segments}}",
    } in blocked_appends
    assert {
        "path": "artifacts",
        "key": "artifact_id",
        "items": "{{collection_validation.validated_artifacts}}",
    } in blocked_appends
    assert "{{collection_result.segments}}" not in json.dumps(blocked_appends)
    assert "{{collection_result.artifacts}}" not in json.dumps(blocked_appends)
    assert steps["save_capture_handoff"]["config"]["history_event"][
        "receipt_ids"
    ] == "{{collection_validation.preserved_receipts}}"


def test_collection_validator_rejects_partial_wrong_scene_and_wrong_asset_results():
    validator = _step("create-product-video-v1", "validate_collection_contract")
    collection_valid = _step("create-product-video-v1", "collection_valid")
    runner = WorkflowRunner()
    plan = _valid_plan()
    inputs = {
        "selected_scenes": plan["scenes"],
        "selected_scene_ids": plan["scene_ids"],
        "granted_scene_ids": plan["scene_ids"],
        "collection_result": _valid_collection_result(plan),
    }

    assert validator["type"] == "code"
    assert validator["config"]["output_var"] == "collection_validation"
    assert validator["config"]["inputs"] == [
        {
            "key": "selected_scenes",
            "value": "{{project.state.plan.scenes}}",
            "type": "json",
        },
        {
            "key": "selected_scene_ids",
            "value": "{{project.state.plan.scene_ids}}",
            "type": "json",
        },
        {
            "key": "granted_scene_ids",
            "value": "{{capture_grant.scope.scene_ids}}",
            "type": "json",
        },
        {
            "key": "collection_result",
            "value": "{{collection_result}}",
            "type": "json",
        },
    ]
    assert validator["next"] == ["collection_valid"]
    assert collection_valid["config"]["expression"] == (
        "collection_validation.valid == true"
    )
    result = _execute_validator(validator["config"]["code"], inputs)
    assert result == {
        "valid": True,
        "errors": [],
        "invalid_scene_ids": [],
        "invalid_artifact_ids": [],
        "rejected_segment_ids": [],
        "rejected_artifact_ids": [],
        "retry_segment_ids": [],
        "validated_segments": inputs["collection_result"]["segments"],
        "validated_artifacts": inputs["collection_result"]["artifacts"],
        "preserved_receipts": inputs["collection_result"]["artifacts"],
        "blocker": None,
    }
    assert runner._evaluate_condition(
        collection_valid, {"collection_validation": result}
    )

    partial = deepcopy(inputs)
    partial["collection_result"]["segments"] = []
    result = _execute_validator(validator["config"]["code"], partial)
    assert result["valid"] is False
    assert result["retry_segment_ids"] == ["scene-1"]
    assert "collection_scene_ids_mismatch" in {
        error["code"] for error in result["errors"]
    }
    assert result["validated_segments"] == []
    assert result["validated_artifacts"] == []
    assert result["preserved_receipts"] == []
    assert result["blocker"]["preserved_receipts"] == []

    duplicate_segment = deepcopy(inputs)
    duplicate_segment["collection_result"]["segments"].append(
        deepcopy(duplicate_segment["collection_result"]["segments"][0])
    )
    result = _execute_validator(validator["config"]["code"], duplicate_segment)
    assert result["valid"] is False
    assert result["validated_segments"] == []
    assert result["validated_artifacts"] == []
    assert result["rejected_segment_ids"] == ["scene-1"]
    assert "duplicate_segment_id" in {
        error["code"] for error in result["errors"]
    }

    wrong_grant = deepcopy(inputs)
    wrong_grant["granted_scene_ids"] = ["scene-other"]
    result = _execute_validator(validator["config"]["code"], wrong_grant)
    assert result["valid"] is False
    assert result["invalid_scene_ids"] == ["scene-other"]
    assert "capture_grant_scene_ids_mismatch" in {
        error["code"] for error in result["errors"]
    }

    wrong_evidence = deepcopy(inputs)
    wrong_evidence["collection_result"]["artifacts"][0]["provenance"][
        "acceptance_evidence"
    ] = []
    result = _execute_validator(validator["config"]["code"], wrong_evidence)
    assert result["valid"] is False
    assert result["invalid_artifact_ids"] == ["scene-1-video"]
    assert "artifact_acceptance_evidence_mismatch" in {
        error["code"] for error in result["errors"]
    }
    assert result["validated_segments"] == []
    assert [
        artifact["artifact_id"] for artifact in result["validated_artifacts"]
    ] == ["scene-1-image"]
    assert result["preserved_receipts"] == result["validated_artifacts"]
    assert result["rejected_artifact_ids"] == ["scene-1-video"]

    wrong_scene = deepcopy(inputs)
    wrong_artifact = wrong_scene["collection_result"]["artifacts"][0]
    wrong_artifact["scene_id"] = "scene-other"
    wrong_artifact["provenance"]["scene_id"] = "scene-other"
    result = _execute_validator(validator["config"]["code"], wrong_scene)
    assert result["valid"] is False
    assert result["invalid_artifact_ids"] == ["scene-1-video"]
    assert "artifact_scene_mismatch" in {
        error["code"] for error in result["errors"]
    }
    assert [
        artifact["artifact_id"] for artifact in result["preserved_receipts"]
    ] == ["scene-1-image"]

    wrong_asset = deepcopy(inputs)
    wrong_asset["collection_result"]["artifacts"] = [
        artifact
        for artifact in wrong_asset["collection_result"]["artifacts"]
        if artifact["kind"] != "image"
    ]
    result = _execute_validator(validator["config"]["code"], wrong_asset)
    assert result["valid"] is False
    assert "required_asset_missing" in {
        error["code"] for error in result["errors"]
    }
    assert result["retry_segment_ids"] == ["scene-1"]
    assert result["validated_segments"] == []
    assert [
        artifact["artifact_id"] for artifact in result["validated_artifacts"]
    ] == ["scene-1-video"]


def test_collection_validator_accepts_verified_browser_capture_receipts():
    validator = _step("create-product-video-v1", "validate_collection_contract")
    plan = _valid_plan()
    collection = _valid_collection_result(plan)
    scenes_by_id = {scene["scene_id"]: scene for scene in plan["scenes"]}

    for artifact in collection["artifacts"]:
        provenance = artifact["provenance"]
        provenance.pop("scene_id")
        provenance.pop("acceptance_evidence")
        provenance["source_url"] = scenes_by_id[artifact["scene_id"]]["target_page"]
        if artifact["kind"] == "video":
            provenance["acceptance_verified"] = True
        else:
            provenance["bitmap_acceptance_verified"] = True

    result = _execute_validator(
        validator["config"]["code"],
        {
            "selected_scenes": plan["scenes"],
            "selected_scene_ids": plan["scene_ids"],
            "granted_scene_ids": plan["scene_ids"],
            "collection_result": collection,
        },
    )

    assert result["valid"] is True
    assert result["errors"] == []
    assert result["validated_segments"] == collection["segments"]
    assert result["validated_artifacts"] == collection["artifacts"]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("target_page", "/dashboard"),
        ("precondition", {"url": "/dashboard"}),
        (
            "ordered_actions",
            [
                {
                    "action": "click_element",
                    "arguments": {"element_id": "other"},
                    "side_effect": True,
                }
            ],
        ),
        ("expected_visual_state", {"text": "Unrelated state"}),
        ("canonical_narration", "A substituted product claim."),
        ("required_asset_types", ["screenshot", "recording"]),
        ("acceptance_evidence", list(reversed(_valid_scene()["acceptance_evidence"]))),
        ("target_duration_seconds", 9),
        ("dependencies", ["scene-other"]),
        ("recovery", "Open an unrelated page"),
        ("privacy", ["Show customer data"]),
    ],
)
def test_collection_validator_rejects_mutated_approved_scene_contract(
    field,
    replacement,
):
    validator = _step("create-product-video-v1", "validate_collection_contract")
    plan = _valid_plan()
    collection = _valid_collection_result(plan)
    collection["segments"][0][field] = replacement

    result = _execute_validator(
        validator["config"]["code"],
        {
            "selected_scenes": plan["scenes"],
            "selected_scene_ids": plan["scene_ids"],
            "granted_scene_ids": plan["scene_ids"],
            "collection_result": collection,
        },
    )

    assert result["valid"] is False
    assert result["validated_segments"] == []
    assert result["validated_artifacts"] == []
    assert result["preserved_receipts"] == []
    assert result["rejected_segment_ids"] == ["scene-1"]
    assert result["rejected_artifact_ids"] == [
        "scene-1-image",
        "scene-1-video",
    ]
    mismatch = next(
        error
        for error in result["errors"]
        if error["code"] == "segment_contract_mismatch"
    )
    assert mismatch["scene_id"] == "scene-1"
    assert field in mismatch["fields"]
    assert result["blocker"]["observed_state"]["rejected_segment_ids"] == [
        "scene-1"
    ]


def test_collection_validator_preserves_valid_receipts_from_blocked_segment():
    validator = _step("create-product-video-v1", "validate_collection_contract")
    plan = _valid_plan()
    collection = _valid_collection_result(plan)
    video_artifact = collection["artifacts"][0]
    segment = collection["segments"][0]
    segment["status"] = "blocked"
    segment["artifact_ids"] = [video_artifact["artifact_id"]]
    collection.update(
        {
            "collection_complete": False,
            "artifacts": [video_artifact],
            "retry_segment_ids": ["scene-1"],
            "blocker": {
                "scene_id": "scene-1",
                "observed_state": {"message": "Screenshot capture failed"},
                "required_change": "Retry the screenshot capture.",
                "preserved_receipts": [video_artifact],
            },
        }
    )

    result = _execute_validator(
        validator["config"]["code"],
        {
            "selected_scenes": plan["scenes"],
            "selected_scene_ids": plan["scene_ids"],
            "granted_scene_ids": plan["scene_ids"],
            "collection_result": collection,
        },
    )

    assert result["valid"] is False
    assert result["validated_segments"] == [segment]
    assert result["validated_artifacts"] == [video_artifact]
    assert result["preserved_receipts"] == [video_artifact]
    assert result["rejected_segment_ids"] == []
    assert result["rejected_artifact_ids"] == []


def test_collection_validator_filters_nested_blocker_receipts():
    validator = _step("create-product-video-v1", "validate_collection_contract")
    plan = _valid_plan()
    collection = _valid_collection_result(plan)
    video_artifact = collection["artifacts"][0]
    rejected_receipt = deepcopy(collection["artifacts"][1])
    rejected_receipt["artifact_id"] = "rejected-image"
    rejected_receipt["scene_id"] = "scene-other"
    rejected_receipt["provenance"]["scene_id"] = "scene-other"
    blocker = {
        "scene_id": "scene-1",
        "observed_state": {"message": "Screenshot capture failed"},
        "required_change": "Retry the screenshot capture.",
        "preserved_receipts": [video_artifact, rejected_receipt],
    }
    segment = collection["segments"][0]
    segment.update(
        {
            "status": "blocked",
            "artifact_ids": [video_artifact["artifact_id"]],
            "blocker": blocker,
        }
    )
    collection.update(
        {
            "collection_complete": False,
            "artifacts": [video_artifact],
            "retry_segment_ids": ["scene-1"],
            "blocker": blocker,
        }
    )

    result = _execute_validator(
        validator["config"]["code"],
        {
            "selected_scenes": plan["scenes"],
            "selected_scene_ids": plan["scene_ids"],
            "granted_scene_ids": plan["scene_ids"],
            "collection_result": collection,
        },
    )

    assert result["validated_segments"][0]["blocker"]["preserved_receipts"] == [
        video_artifact
    ]
    assert result["preserved_receipts"] == [video_artifact]
    assert result["rejected_artifact_ids"] == ["rejected-image"]


def test_collection_blocker_schema_requires_structured_operator_correction():
    blocker_schema = template.COLLECTION_RESULT_SCHEMA["properties"]["blocker"]

    assert blocker_schema["anyOf"][0]["required"] == [
        "scene_id",
        "observed_state",
        "required_change",
        "preserved_receipts",
    ]


def test_successful_batch_capture_upserts_durable_artifact_receipts():
    use_collection = _step("create-product-video-v1", "use_collection_result")
    checkpoint = _step("create-product-video-v1", "save_collection_checkpoint")

    assert use_collection["config"]["set"]["effective_collection"] == {
        "manifest_path": "{{collection_result.manifest_path}}",
        "segments": "{{collection_validation.validated_segments}}",
        "artifacts": "{{collection_validation.validated_artifacts}}",
    }

    assert {
        "path": "artifacts",
        "key": "artifact_id",
        "items": "{{effective_collection.artifacts}}",
    } in checkpoint["config"]["list_appends"]
    assert "{{collection_result.segments}}" not in json.dumps(
        checkpoint["config"]["list_appends"]
    )
    assert "{{collection_result.artifacts}}" not in json.dumps(
        checkpoint["config"]["list_appends"]
    )


def test_collection_retry_and_reuse_considers_only_validated_project_artifacts():
    collector = _step("create-product-video-v1", "collect_assets")["config"]

    assert (
        "For retry or reuse, consider only validator-approved artifacts already "
        "persisted in project.state.artifacts"
    ) in collector["input"]
    assert (
        "Never reuse raw artifacts or preserved receipts from collection_result"
    ) in collector["input"]
    assert "validator-approved project artifacts" in collector[
        "forced_tool_calls"
    ][0]["arguments"]["input"]


def test_planning_maps_only_authoritative_must_show_items_to_scene_evidence():
    planning = next(
        spec for spec in template.WORKFLOW_SPECS
        if spec["slug"] == "plan-product-video-v1"
    )
    plan_step = next(step for step in planning["steps"] if step["id"] == "plan_video")
    prompt = plan_step["config"]["input"]

    assert (
        "Every request.must_show item must map to one or more explicit scene_id values "
        "and acceptance_evidence entries"
    ) in prompt
    assert "Create generated video scenes only for request.must_show" in prompt
    assert "No product claim may appear in canonical_narration without an evidence-backed scene" in prompt
    assert (
        "Knowledge completeness and final playback are acceptance checks, not generated video scenes"
    ) in prompt


def test_plan_contract_validator_executes_cross_field_coverage_checks():
    steps = _steps_by_id("create-product-video-v1")
    validator = steps["validate_plan_contract"]
    guard = steps["plan_coverage_complete"]
    runner = WorkflowRunner()

    assert steps["plan_video"]["next"] == ["validate_plan_contract"]
    assert validator["type"] == "code"
    assert validator["config"]["language"] == "python"
    assert validator["config"]["output_format"] == "json"
    assert validator["config"]["output_var"] == "plan_validation"
    assert validator["config"]["inputs"] == [
        {"key": "request", "value": "{{request}}", "type": "json"},
        {"key": "plan", "value": "{{plan}}", "type": "json"},
    ]
    assert validator["next"] == ["plan_coverage_complete"]
    assert guard["config"]["expression"] == "plan_validation.valid == true"
    assert guard["true_next"] == ["save_plan"]
    assert guard["false_next"] == ["save_plan_coverage_handoff"]
    assert steps["save_plan"]["next"] == ["approve_plan"]

    code = validator["config"]["code"]
    valid = _execute_validator(
        code,
        {"request": _valid_request(), "plan": _valid_plan()},
    )
    assert valid == {
        "valid": True,
        "errors": [],
        "missing_requirements": [],
        "invalid_scene_ids": [],
        "invalid_evidence_ids": [],
    }
    assert runner._evaluate_condition(guard, {"plan_validation": valid})

    mismatched = _valid_plan()
    mismatched["must_show_coverage"][0]["requirement"] = "Unrequested claim"
    result = _execute_validator(
        code,
        {"request": _valid_request(), "plan": mismatched},
    )
    assert result["valid"] is False
    assert result["missing_requirements"] == ["Submit a request"]
    assert not runner._evaluate_condition(guard, {"plan_validation": result})

    unknown_scene = _valid_plan()
    unknown_scene["must_show_coverage"][0]["scene_ids"] = ["scene-missing"]
    result = _execute_validator(
        code,
        {"request": _valid_request(), "plan": unknown_scene},
    )
    assert result["valid"] is False
    assert result["invalid_scene_ids"] == ["scene-missing"]

    missing_scene_evidence = _valid_plan()
    missing_scene_evidence["scenes"][0]["acceptance_evidence"] = [
        missing_scene_evidence["scenes"][0]["acceptance_evidence"][1]
    ]
    result = _execute_validator(
        code,
        {"request": _valid_request(), "plan": missing_scene_evidence},
    )
    assert result["valid"] is False
    assert "scene_acceptance_evidence_mismatch" in {
        error["code"] for error in result["errors"]
    }

    overlong_narration = _valid_plan()
    overlong_narration["scenes"][0]["canonical_narration"] = "这是明显超过八秒画面容量的旁白内容。" * 8
    overlong_narration["canonical_narration"] = overlong_narration["scenes"][0][
        "canonical_narration"
    ]
    result = _execute_validator(
        code,
        {"request": _valid_request(), "plan": overlong_narration},
    )
    assert result["valid"] is False
    assert result["invalid_scene_ids"] == ["scene-1"]
    timing_error = next(
        error
        for error in result["errors"]
        if error["code"] == "scene_narration_duration_exceeded"
    )
    assert timing_error["scene_id"] == "scene-1"
    assert timing_error["estimated_narration_seconds"] > 8
    assert timing_error["available_scene_seconds"] == 8

    extra_scene = _valid_plan()
    unmapped = deepcopy(extra_scene["scenes"][0])
    unmapped["scene_id"] = "scene-extra"
    extra_scene["scene_ids"].append("scene-extra")
    extra_scene["scenes"].append(unmapped)
    result = _execute_validator(
        code,
        {"request": _valid_request(), "plan": extra_scene},
    )
    assert result["valid"] is False
    assert result["invalid_scene_ids"] == ["scene-extra"]
    assert "plan_scene_scope_mismatch" in {
        error["code"] for error in result["errors"]
    }

    retry_state = steps["save_plan_coverage_handoff"]["config"]["patch"][
        "retry_state"
    ]
    assert retry_state["retry_from_step_id"] == "plan_video"
    assert "request" in retry_state["editable_input_schema"]["properties"]
    assert steps["save_plan_coverage_handoff"]["next"] == [
        "build_plan_coverage_handoff"
    ]
    assert steps["build_plan_coverage_handoff"]["next"] == ["needs_input"]


def test_revision_coverage_uses_the_executable_plan_validator():
    steps = {
        step["id"]: step
        for step in _workflow("revise-product-video-v1")["steps"]
    }
    validator = steps["validate_revision_plan_contract"]
    guard = steps["revision_coverage_complete"]
    runner = WorkflowRunner()

    assert steps["plan_revision"]["next"] == ["validate_revision_plan_contract"]
    assert validator["type"] == "code"
    assert validator["config"]["code"] == template.PLAN_CONTRACT_VALIDATOR_CODE
    assert validator["config"]["inputs"] == [
        {
            "key": "request",
            "value": "{{project.state.request}}",
            "type": "json",
        },
        {
            "key": "plan",
            "value": "{{revision_plan.updated_plan}}",
            "type": "json",
        },
    ]
    assert validator["next"] == ["revision_coverage_complete"]
    assert guard["config"]["expression"] == "plan_validation.valid == true"
    assert guard["true_next"] == ["post_only"]
    assert guard["false_next"] == ["save_revision_coverage_handoff"]
    result = _execute_validator(
        validator["config"]["code"],
        {"request": _valid_request(), "plan": _valid_plan()},
    )
    assert runner._evaluate_condition(guard, {"plan_validation": result})

    retry_state = steps["save_revision_coverage_handoff"]["config"]["patch"][
        "retry_state"
    ]
    assert retry_state["retry_from_step_id"] == "plan_revision"
    assert steps["build_revision_coverage_handoff"]["next"] == ["needs_input"]


def test_planning_keeps_post_capture_outputs_out_of_browser_scenes():
    planning = next(
        spec for spec in template.WORKFLOW_SPECS
        if spec["slug"] == "plan-product-video-v1"
    )
    plan_prompt = next(
        step for step in planning["steps"] if step["id"] == "plan_video"
    )["config"]["input"]
    revise = next(
        spec for spec in template.WORKFLOW_SPECS
        if spec["slug"] == "revise-product-video-v1"
    )
    revision_prompt = next(
        step for step in revise["steps"] if step["id"] == "plan_revision"
    )["config"]["input"]

    for prompt in (plan_prompt, revision_prompt):
        assert "states available before post-production" in prompt
        assert "downstream workflow outputs" in prompt
        assert "never put them in a scene precondition" in prompt


def test_planning_and_capture_require_a_verified_product_route():
    planning = next(
        spec for spec in template.WORKFLOW_SPECS
        if spec["slug"] == "plan-product-video-v1"
    )
    plan = next(step for step in planning["steps"] if step["id"] == "plan_video")
    discover = _step("plan-product-video-v1", "explore_product")
    capture_step = _step("create-product-video-v1", "collect_assets")
    chrome_input = capture_step["config"]["forced_tool_calls"][0]["arguments"]["input"]

    assert "exact route and control label" in discover["config"]["input"]
    assert "Use only routes and controls observed in discovery" in plan["config"]["input"]
    assert "authoritative product source {{project.state.product_source}}" in capture_step["config"]["input"]
    assert "similarly named global or sidebar control" in chrome_input


def test_discovery_opens_reversible_option_menus_to_verify_entrypoints():
    discover = _step("plan-product-video-v1", "explore_product")
    prompt = discover["config"]["input"]
    chrome_input = discover["config"]["forced_tool_calls"][0]["arguments"]["input"]

    for value in (prompt, chrome_input):
        assert "open reversible option menus" in value
        assert "enumerate every visible option" in value


def test_discovery_stops_after_the_requested_product_evidence_is_verified():
    discover = _step("plan-product-video-v1", "explore_product")
    prompt = discover["config"]["input"]
    chrome_input = discover["config"]["forced_tool_calls"][0]["arguments"]["input"]

    for value in (prompt, chrome_input):
        assert "Stop immediately once every must-show requirement is verified" in value
        assert "Do not inspect unrelated selectors, menus, account controls, or routes" in value
        assert "Readiness depends only on must-show coverage" in value
        assert "Do not open a control when must-show only asks to show its label" in value


def test_product_experience_mapper_reuses_the_preexecuted_chrome_result():
    skill_text = (
        MARKETPLACE_SKILLS_DIR / "product-experience-mapper" / "SKILL.md"
    ).read_text()

    assert "at most one `invoke_skill` call to `chrome`" in skill_text
    assert "pre-executed or forced Chrome result" in skill_text
    assert "do not call Chrome again" in skill_text
    assert "version: 1.2.1" in skill_text


def test_product_video_roles_are_bounded_and_machine_qa_tools_are_bound():
    assert [role.agent_name for role in template.ROLE_SPECS] == [
        "Product Explorer",
        "Video Planner",
        "Scene Collector",
        "Video Producer",
        "Quality Reviewer",
    ]
    role_by_key = {role.service_key: role for role in template.ROLE_SPECS}
    assert set(template.QUALITY_EVIDENCE_TOOLS) == {
        "probe_media",
        "render_frame_samples",
        "analyze_audio",
        "validate_subtitles",
    }
    assert role_by_key["product_video.quality"].tool_bindings == (
        "read_file",
        "generate_file",
    )
    assert "still_to_video" in role_by_key["product_video.production"].tool_bindings
    assert all("demo" not in role.agent_name.lower() for role in template.ROLE_SPECS)


def test_product_video_workflow_surface_and_graphs_match_the_design():
    assert template.USER_WORKFLOW_SLUGS == [
        "create-product-video-v1",
        "plan-product-video-v1",
        "revise-product-video-v1",
    ]
    assert template.INTERNAL_WORKFLOW_SLUGS == []
    assert template.WORKFLOW_SLUGS == template.USER_WORKFLOW_SLUGS
    assert len(template.WORKFLOW_SPECS) == 3
    assert [spec["name"] for spec in template.WORKFLOW_SPECS if spec["user_facing"]] == [
        "Create product video",
        "Plan product video",
        "Revise product video",
    ]
    for spec in template.WORKFLOW_SPECS:
        validation = validate_workflow_steps(spec["steps"])
        assert validation["valid"], (spec["slug"], validation)
        assert all(
            step["type"] not in {"subworkflow", "foreach_subworkflow"}
            for step in spec["steps"]
        )

    create = next(spec for spec in template.WORKFLOW_SPECS if spec["slug"] == "create-product-video-v1")
    plan = next(spec for spec in template.WORKFLOW_SPECS if spec["slug"] == "plan-product-video-v1")
    for spec in (create, plan):
        assert spec["variables"]["request"] == template.DEFAULT_PRODUCT_VIDEO_REQUEST
        assert [item["key"] for item in spec["run_inputs"]] == [
            "product_name",
            "start_url",
            "audience",
            "video_type",
            "promotion_goal",
            "must_show",
            "must_not_show",
            "final_cta",
            "narration_instructions",
            "subtitle_instructions",
            "production_constraints",
            "failure_policy",
            "output_profile",
            "reference_documents",
            "reference_assets",
            "browser_session",
            "source_brief",
        ]
        assert all(item["target"].startswith("request.") for item in spec["run_inputs"])
        assert spec["input_prefill"] == template.PRODUCT_VIDEO_REQUEST_PREFILL
    steps = _steps_by_id("create-product-video-v1")
    assert steps["explore_product"]["type"] == "agent"
    assert steps["plan_video"]["type"] == "agent"
    assert steps["approve_plan"]["type"] == "wait"
    assert steps["approve_plan"]["config"]["review"] == "{{plan}}"
    assert steps["approve_plan"]["config"]["review_title"] == "Product video plan"
    assert steps["plan_approved"]["config"]["expression"] == "plan_decision.choice == 'approve'"
    assert steps["grant_capture"]["type"] == "workflow_action_grant"
    assert steps["collect_assets"]["type"] == "agent"
    assert steps["produce_video"]["type"] == "agent"
    assert steps["review_quality"]["type"] == "agent"
    assert steps["quality_passed"]["true_next"] == ["save_quality_ready"]
    assert steps["save_quality_ready"]["config"]["patch"]["business_outcome"] == (
        "completed"
    )
    assert steps["save_quality_ready"]["next"] == ["build_completed_result"]
    assert steps["build_completed_result"]["next"] == ["done"]
    assert "operator_acceptance" not in steps
    assert "accepted" not in steps
    assert "mark_accepted" not in steps

    planning_steps = {step["id"]: step for step in plan["steps"]}
    assert planning_steps["initialize_project"]["type"] == "workflow_project"

    revise = next(spec for spec in template.WORKFLOW_SPECS if spec["slug"] == "revise-product-video-v1")
    revision_steps = {step["id"]: step for step in revise["steps"]}
    assert revision_steps["revision_approved"]["config"]["expression"] == "revision_decision.choice == 'approve'"
    assert "operator_acceptance" not in revision_steps
    assert "accepted" not in revision_steps
    assert "mark_accepted" not in revision_steps
    assert "retry_segment_ids" in revise["variables"]


def test_create_product_video_is_one_nine_node_business_pipeline():
    create = _workflow("create-product-video-v1")

    assert len(create["steps"]) == 9
    assert [step["id"] for step in create["steps"]] == [
        "start",
        "initialize_project",
        "browser_preflight",
        "explore_product",
        "plan_video",
        "approve_plan",
        "collect_assets",
        "produce_video",
        "review_quality",
    ]
    assert all(step["type"] == "stage" for step in create["steps"][1:])


def test_create_product_video_stage_packing_preserves_every_flat_operation_once():
    create = _workflow("create-product-video-v1")
    flat_steps = template._flat_create_steps()
    terminal_ids = {
        step["id"] for step in flat_steps if step.get("type") == "end"
    }
    expected_operation_ids = [
        step["id"] for step in flat_steps if step.get("type") not in {"trigger", "end"}
    ]
    packed_operations = [
        operation
        for stage in create["steps"]
        for operation in (stage.get("config") or {}).get("operations") or []
    ]

    assert [operation["id"] for operation in packed_operations] == expected_operation_ids
    assert len({operation["id"] for operation in packed_operations}) == len(
        packed_operations
    )
    assert {
        route_name
        for stage in create["steps"]
        for route_name, target in ((stage.get("config") or {}).get("routes") or {}).items()
        if target is None
    } == terminal_ids
    assert all(
        operation["type"] not in {"stage", "subworkflow", "foreach_subworkflow"}
        for operation in packed_operations
    )


def test_actionable_progress_skips_unselected_nodes_before_execution_frontier():
    run = SimpleNamespace(
        definition_snapshot={
            "nodes": [
                {"id": "start", "name": "Start", "type": "trigger"},
                {"id": "route", "name": "Choose route", "type": "condition"},
                {"id": "unused", "name": "Unused branch", "type": "agent"},
                {"id": "work", "name": "Collect assets", "type": "agent"},
                {"id": "finish", "name": "Input required", "type": "end"},
            ],
        },
        step_results={
            "start": {"status": "completed"},
            "route": {"status": "completed", "next_override": ["work"]},
            "work": {"status": "completed"},
        },
        status="completed",
        current_step_id="finish",
        variables={
            "project": {
                "state": {
                    "business_outcome": "needs_input",
                },
            },
        },
    )

    assert [step["status"] for step in workflow_progress_steps(run)] == [
        "completed",
        "completed",
        "skipped",
        "completed",
        "pending",
    ]


def test_product_video_project_and_phase_checkpoints_are_traceable():
    required_state = set(template.PRODUCT_VIDEO_PROJECT_SCHEMA["required"])
    assert {
        "project_root",
        "ledger_path",
        "artifact_manifest_path",
        "current_phase",
        "business_outcome",
        "segments",
        "checkpoints",
        "retry_state",
        "final_artifacts",
        "history",
    } <= required_state

    create_steps = _steps_by_id("create-product-video-v1")
    assert create_steps["collect_assets"]["next"] == ["validate_collection_contract"]
    assert create_steps["produce_video"]["next"] == ["validate_production_contract"]
    assert create_steps["review_quality"]["next"] == ["validate_quality_contract"]

    for step_id in (
        "assign_project_storage",
        "save_discovery",
        "save_plan",
        "save_collection_checkpoint",
        "save_capture_handoff",
        "save_production_checkpoint",
        "save_production_handoff",
        "save_quality_ready",
        "save_revision_required",
    ):
        assert "history_event" in create_steps[step_id]["config"], step_id

    retry_state = create_steps["save_capture_handoff"]["config"]["patch"]["retry_state"]
    assert set(retry_state) == {
        "phase",
        "step_id",
        "scene_id",
        "segment_ids",
        "observed_state",
        "observed_problem",
        "required_change",
        "editable_input_schema",
        "preserved_receipts",
        "retry_from_step_id",
    }


def test_request_recovery_edits_the_declared_request_variable():
    create_steps = _steps_by_id("create-product-video-v1")

    for step_id in ("save_discovery_handoff", "save_plan_revision_required"):
        schema = create_steps[step_id]["config"]["patch"]["retry_state"][
            "editable_input_schema"
        ]
        assert "request" in schema["properties"], step_id
        assert "product_name" in schema["properties"]["request"]["properties"]
        assert "product_name" not in schema["properties"]


def test_discovery_executes_the_read_only_route_and_returns_an_actionable_handoff():
    explore = _step("create-product-video-v1", "explore_product")
    prompt = explore["config"]["input"]
    forced_prompt = explore["config"]["forced_tool_calls"][0]["arguments"]["input"]

    for value in (prompt, forced_prompt):
        assert "Execute the complete reversible route now" in value
        assert "Do not return navigation steps that you did not perform" in value
        assert "read_page with filter" in value
        assert "exact distinctive label" in value
        assert "Do not inspect adjacent or unrelated controls" in value
        assert "Do not use computer, coordinates, or screenshot fallback" in value
        assert "timeout of at most 10 seconds" in value
    assert "authoritative request, including every must-show and must-not-show item" in forced_prompt
    assert "{{request}}" in forced_prompt

    save_handoff = _step("create-product-video-v1", "save_discovery_handoff")
    retry_state = save_handoff["config"]["patch"]["retry_state"]
    assert retry_state["step_id"] == "explore_product"
    assert retry_state["observed_problem"] == "{{discovered_journey.gaps}}"
    assert retry_state["required_change"]

    handoff = _step("create-product-video-v1", "build_discovery_handoff")["config"]["set"]["input"]
    assert handoff["project_id"] == "{{project.project_id}}"
    assert handoff["project_root"] == "{{project.state.project_root}}"
    assert handoff["observed_problem"] == "{{project.state.retry_state.observed_problem}}"
    assert handoff["required_change"] == "{{project.state.retry_state.required_change}}"
    assert handoff["preserved_receipts"] == "{{project.state.retry_state.preserved_receipts}}"


def test_flat_workflows_keep_request_as_the_single_authoritative_input():
    for workflow_slug in ("create-product-video-v1", "plan-product-video-v1"):
        workflow = _workflow(workflow_slug)
        normalize = next(
            step
            for step in _executable_steps(workflow_slug)
            if step["id"] == "normalize_request"
        )
        assert normalize["config"]["output_var"] == "request"
        assert "normalized_request" not in json.dumps(workflow)

    mark_discovering = _step("create-product-video-v1", "mark_discovering")
    patch = mark_discovering["config"]["patch"]
    assert patch["request"] == "{{request}}"
    assert patch["product_source"]["start_url"] == "{{request.start_url}}"
    assert patch["output_profile"] == "{{request.output_profile}}"


def test_plan_checkpoint_persists_the_authoritative_retry_request():
    for workflow_slug in ("create-product-video-v1", "plan-product-video-v1"):
        patch = _step(workflow_slug, "save_plan")["config"]["patch"]

        assert patch["request"] == "{{request}}", workflow_slug
        assert patch["product_source"] == {
            "start_url": "{{request.start_url}}",
            "browser_session": "{{request.browser_session}}",
        }, workflow_slug
        assert patch["output_profile"] == "{{request.output_profile}}", workflow_slug


def test_planner_writes_only_relative_children_inside_the_project_bundle():
    prompt = _step("plan-product-video-v1", "plan_video")["config"]["input"]

    assert "name exactly equal to {{project.state.project_root}}" in prompt
    assert "relative child paths" in prompt
    assert "Never repeat project_root" in prompt


def test_producer_uses_document_receipts_and_never_returns_generic_failure():
    produce = _step("create-product-video-v1", "produce_video")
    prompt = produce["config"]["input"]

    assert "Pass recording document_id values directly to merge_videos" in prompt
    assert "Never pass a display path beginning with Knowledge/" in prompt
    assert "Never return empty scene IDs or a narration-only generic cause" in prompt
    assert "Never mark a silent or unaligned final video ready" in prompt
    assert "Honor project.state.retry_state.required_change" in prompt
    assert "use the planned screenshot as that scene's visual source" in prompt


def test_producer_uses_one_take_measured_narration_and_explicit_scene_intervals():
    prompt = _step("create-product-video-v1", "produce_video")["config"]["input"]

    assert "one continuous final narration take from plan.canonical_narration" in prompt
    assert "Do not synthesize or stitch per-scene narration" in prompt
    assert (
        "final narration audio, canonical transcript, and explicit visual scene intervals "
        "to align_subtitles"
    ) in prompt
    assert "measured semantic sentence/word timestamps" in prompt
    assert "Never use proportional cue scaling" in prompt
    assert "Map aligned sentence intervals to visual scene boundaries" in prompt
    assert "Produce ASS subtitles" in prompt
    assert "rebuild the clean picture master" in prompt
    assert "measured narration boundaries" in prompt


def test_every_planner_budgets_narration_to_scene_duration():
    for workflow_slug in ("create-product-video-v1", "plan-product-video-v1"):
        prompt = _step(workflow_slug, "plan_video")["config"]["input"]

        assert "Budget every scene's canonical narration" in prompt, workflow_slug
        assert "within that scene's target_duration_seconds" in prompt, workflow_slug
        assert "shorten copy" in prompt, workflow_slug


def test_producer_requires_visual_retake_beyond_narration_deficit_threshold():
    prompt = _step("create-product-video-v1", "produce_video")["config"]["input"]

    assert "min(0.75 seconds, 10% of the required narration span)" in prompt
    assert "directly from timeline.visual_scene_intervals" in prompt
    assert "return retake_required with deficient_scene_ids" in prompt
    assert "Never time-stretch unrelated video" in prompt


def test_production_schema_requires_measured_scene_alignment():
    validator = Draft202012Validator(template.PRODUCTION_RESULT_SCHEMA)
    valid = _valid_production_result()
    validator.validate(valid)

    invalid_results = []
    for key, value in (
        ("similarity", 0.89),
        ("coverage", 0.94),
        ("measured_timestamps", False),
        ("timing_sources", ["proportional_duration_scale"]),
        ("transcription_model", None),
    ):
        invalid = deepcopy(valid)
        invalid["timeline"]["alignment_metrics"][key] = value
        invalid_results.append(invalid)

    for key in ("missing_scene_ids", "missing_interval_scene_ids"):
        invalid = deepcopy(valid)
        invalid["timeline"]["alignment_metrics"]["scene_coverage"][key] = [
            "scene-1"
        ]
        invalid_results.append(invalid)

    for timeline_key in ("visual_scene_intervals", "scene_boundaries"):
        invalid = deepcopy(valid)
        invalid["timeline"][timeline_key] = []
        invalid_results.append(invalid)

    invalid = deepcopy(valid)
    del invalid["timeline"]["alignment_metrics"]
    invalid_results.append(invalid)

    for invalid in invalid_results:
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_production_schema_allows_partial_sentence_alignment_at_threshold():
    result = _valid_production_result()
    metrics = result["timeline"]["alignment_metrics"]
    metrics["coverage"] = 0.95
    metrics["missing_sentence_indexes"] = [20]

    Draft202012Validator(template.PRODUCTION_RESULT_SCHEMA).validate(result)


def test_production_schema_matches_align_subtitles_and_structured_blockers():
    schema = template.PRODUCTION_RESULT_SCHEMA
    validator = Draft202012Validator(schema)

    assert schema["properties"]["status"]["enum"] == [
        "ready",
        "retake_required",
        "blocked",
    ]
    validator.validate(_valid_production_result())
    validator.validate(_blocked_production_result())

    metrics = _valid_production_result()["timeline"]["alignment_metrics"]
    assert metrics["timing_sources"] == ["measured_stt_words"]
    assert metrics["transcription_model"] == "groq/whisper-large-v3"
    assert metrics["scene_coverage"] == {
        "coverage": 1.0,
        "covered_scene_ids": ["scene-1"],
        "missing_scene_ids": [],
        "missing_interval_scene_ids": [],
        "unmapped_scene_ids": [],
    }

    malformed = _blocked_production_result()
    del malformed["blocker"]["preserved_receipts"]
    with pytest.raises(ValidationError):
        validator.validate(malformed)


@pytest.mark.parametrize(
    ("timeline_key", "wrong_kind"),
    [
        ("narration", "video"),
        ("subtitles", "audio"),
        ("final_video", "subtitle"),
    ],
)
def test_production_schema_rejects_wrong_artifact_kinds(timeline_key, wrong_kind):
    invalid = _valid_production_result()
    invalid["timeline"][timeline_key]["kind"] = wrong_kind

    with pytest.raises(ValidationError):
        Draft202012Validator(template.PRODUCTION_RESULT_SCHEMA).validate(invalid)


def test_production_retake_requires_scene_ids_and_computed_deficit_evidence():
    validator = Draft202012Validator(template.PRODUCTION_RESULT_SCHEMA)
    valid_retake = _retake_required_production_result()
    validator.validate(valid_retake)

    for field in ("deficient_scene_ids", "retry_segment_ids", "deficit_evidence"):
        invalid = deepcopy(valid_retake)
        invalid[field] = []
        with pytest.raises(ValidationError):
            validator.validate(invalid)

    blocked = _blocked_production_result()
    validator.validate(blocked)

    fabricated_blocker_deficit = deepcopy(blocked)
    fabricated_blocker_deficit["deficient_scene_ids"] = ["scene-1"]
    with pytest.raises(ValidationError):
        validator.validate(fabricated_blocker_deficit)


def test_production_validator_recomputes_deficits_and_alignment_metrics():
    step = _step("create-product-video-v1", "validate_production_contract")
    runner = WorkflowRunner()
    code = step["config"]["code"]

    assert step["type"] == "code"
    assert step["config"]["output_var"] == "production_validation"
    assert step["config"]["inputs"] == [
        {
            "key": "production_result",
            "value": "{{production_result}}",
            "type": "json",
        },
        {
            "key": "plan",
            "value": "{{project.state.plan}}",
            "type": "json",
        },
    ]
    valid = _execute_validator(
        code,
        {"production_result": _valid_production_result(), "plan": _valid_plan()},
    )
    assert valid["valid"] is True

    deficient_ready = _valid_production_result()
    deficient_ready["timeline"]["visual_scene_intervals"][0][
        "visual_end_seconds"
    ] = 1
    result = _execute_validator(
        code,
        {"production_result": deficient_ready, "plan": _valid_plan()},
    )
    assert result["valid"] is False
    assert "ready_visual_deficit" in {error["code"] for error in result["errors"]}

    for path, value in (
        (("timeline", "alignment_metrics", "similarity"), 0.89),
        (("timeline", "alignment_metrics", "coverage"), 0.94),
        (("timeline", "alignment_metrics", "measured_timestamps"), False),
        (
            ("timeline", "alignment_metrics", "scene_coverage", "missing_scene_ids"),
            ["scene-1"],
        ),
        (("timeline", "visual_scene_intervals"), []),
        (("timeline", "narration", "kind"), "video"),
    ):
        invalid = _valid_production_result()
        target = invalid
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        result = _execute_validator(
            code,
            {"production_result": invalid, "plan": _valid_plan()},
        )
        assert result["valid"] is False, path

    short_span = _retake_required_production_result()
    short_span["timeline"]["visual_scene_intervals"][0].update(
        {
            "visual_end_seconds": 0.8,
            "narration_end_seconds": 1.0,
        }
    )
    evidence = short_span["deficit_evidence"][0]
    evidence.update(
        {
            "required_narration_span_seconds": 1.0,
            "available_visual_duration_seconds": 0.8,
            "deficit_seconds": 0.2,
            "allowed_deficit_seconds": 0.1,
        }
    )
    result = _execute_validator(
        code,
        {"production_result": short_span, "plan": _valid_plan()},
    )
    assert result["valid"] is True

    wrong_threshold = deepcopy(short_span)
    wrong_threshold["deficit_evidence"][0]["allowed_deficit_seconds"] = 0.75
    result = _execute_validator(
        code,
        {"production_result": wrong_threshold, "plan": _valid_plan()},
    )
    assert result["valid"] is False
    assert "allowed_deficit_mismatch" in {error["code"] for error in result["errors"]}

    timeline_false_evidence = _retake_required_production_result()
    timeline_false_evidence["deficit_evidence"][0].update(
        {
            "required_narration_span_seconds": 7,
            "available_visual_duration_seconds": 5,
            "deficit_seconds": 2,
            "allowed_deficit_seconds": 0.7,
            "exceeds_threshold": True,
        }
    )
    result = _execute_validator(
        code,
        {
            "production_result": timeline_false_evidence,
            "plan": _valid_plan(),
        },
    )
    assert result["valid"] is False
    assert "retake_evidence_mismatch" in {
        error["code"] for error in result["errors"]
    }

    wrong_ids = deepcopy(short_span)
    wrong_ids["retry_segment_ids"] = ["scene-other"]
    result = _execute_validator(
        code,
        {"production_result": wrong_ids, "plan": _valid_plan()},
    )
    assert result["valid"] is False
    assert result["invalid_scene_ids"] == ["scene-other"]
    assert "retake_scene_ids_mismatch" in {error["code"] for error in result["errors"]}

    blocked = _execute_validator(
        code,
        {"production_result": _blocked_production_result(), "plan": _valid_plan()},
    )
    assert blocked["valid"] is True

    guard = _step("create-product-video-v1", "production_contract_valid")
    assert runner._evaluate_condition(guard, {"production_validation": valid})
    assert not runner._evaluate_condition(
        guard,
        {"production_validation": result},
    )


def test_production_validator_requires_ass_subtitles_for_ready_video():
    validator = _step("create-product-video-v1", "validate_production_contract")
    production = _valid_production_result()
    subtitles = production["timeline"]["subtitles"]
    subtitles.update(
        {
            "fs_path": "production/subtitles.srt",
            "mime_type": "application/x-subrip",
            "provenance": {"format": "srt"},
        }
    )

    result = _execute_validator(
        validator["config"]["code"],
        {"production_result": production, "plan": _valid_plan()},
    )

    assert result["valid"] is False
    assert "ready_subtitles_must_be_ass" in {
        error["code"] for error in result["errors"]
    }


def test_production_validator_rejects_narration_outside_visual_scene_interval():
    validator = _step("create-product-video-v1", "validate_production_contract")
    production = _valid_production_result()
    production["timeline"]["visual_scene_intervals"][0].update(
        {
            "visual_start_seconds": 1,
            "narration_start_seconds": 0,
            "narration_end_seconds": 7,
        }
    )

    result = _execute_validator(
        validator["config"]["code"],
        {"production_result": production, "plan": _valid_plan()},
    )

    assert result["valid"] is False
    assert "narration_outside_visual_scene_interval" in {
        error["code"] for error in result["errors"]
    }


def test_production_handoff_routes_deficient_scenes_back_to_collection():
    steps = _steps_by_id("create-product-video-v1")
    retry_state = steps["save_production_handoff"]["config"]["patch"][
        "retry_state"
    ]

    assert retry_state["segment_ids"] == "{{production_result.retry_segment_ids}}"
    assert retry_state["observed_problem"] == "{{production_result.deficit_evidence}}"
    assert retry_state["required_change"] == "{{production_result.required_change}}"
    assert retry_state["retry_from_step_id"] == "collect_assets"
    assert retry_state["editable_input_schema"]["required"] == [
        "retry_segment_ids"
    ]

    handoff = steps["build_production_handoff"]["config"]["set"]["input"]
    assert handoff["retry_from_step_id"] == "collect_assets"
    assert handoff["retry_segment_ids"] == (
        "{{production_result.retry_segment_ids}}"
    )


def test_quality_requires_scene_evidence_measured_sync_and_compact_subtitles():
    review = _step("create-product-video-v1", "review_quality")
    prompt = review["config"]["input"]

    assert review["config"]["tools"] == ["read_file", "generate_file"]
    assert "Treat the supplied probe, audio, subtitle, and frame results as authoritative" in prompt
    assert "acceptance evidence for every request.must_show scene" in prompt
    assert "Probe final video and narration audio" in prompt
    assert "52 px at 1080p" in prompt
    assert "no more than two lines" in prompt
    assert "outline (2 px at 1080p)" in prompt
    assert "Shadow=0" in prompt
    assert "bottom-safe margin" in prompt
    assert "measured cue-to-scene sync" in prompt
    assert "Open and inspect every sampled frame bitmap with read_file" in prompt
    assert "Never infer visible content from URLs, receipt metadata" in prompt
    assert "must appear in at least one inspected final-video frame" in prompt
    assert "within that scene's measured narration interval" in prompt
    assert "Only cite frame document IDs that visibly satisfy" in prompt
    assert "explicitly permits local test data" in prompt
    assert "John Doe" in prompt
    assert "credentials, tokens, email addresses, phone numbers" in prompt


def test_quality_validator_executes_authoritative_scene_and_evidence_checks():
    validator = _step("create-product-video-v1", "validate_quality_contract")
    quality_passed = _step("create-product-video-v1", "quality_passed")
    runner = WorkflowRunner()
    inputs = {
        "request": _valid_request(),
        "plan": _valid_plan(),
        "quality_result": _valid_quality_result(),
        "artifacts": [_valid_artifact()],
        "timeline": _valid_production_result()["timeline"],
        "media_probe": _valid_media_probe(),
        "audio_analysis": _valid_audio_analysis(),
        "subtitle_analysis": _valid_subtitle_analysis(),
        "frame_evidence": _valid_frame_evidence(),
    }

    assert validator["type"] == "code"
    assert validator["config"]["output_var"] == "quality_validation"
    assert validator["config"]["inputs"] == [
        {
            "key": "request",
            "value": "{{project.state.request}}",
            "type": "json",
        },
        {
            "key": "plan",
            "value": "{{project.state.plan}}",
            "type": "json",
        },
        {
            "key": "quality_result",
            "value": "{{quality_result}}",
            "type": "json",
        },
        {
            "key": "artifacts",
            "value": "{{project.state.artifacts}}",
            "type": "json",
        },
        {
            "key": "timeline",
            "value": "{{project.state.timeline}}",
            "type": "json",
        },
        {
            "key": "media_probe",
            "value": "{{media_probe}}",
            "type": "json",
        },
        {
            "key": "audio_analysis",
            "value": "{{audio_analysis}}",
            "type": "json",
        },
        {
            "key": "subtitle_analysis",
            "value": "{{subtitle_analysis}}",
            "type": "json",
        },
        {
            "key": "frame_evidence",
            "value": "{{frame_evidence}}",
            "type": "json",
        },
    ]
    assert validator["next"] == ["quality_passed"]
    assert quality_passed["config"]["expression"] == "quality_validation.valid == true"
    result = _execute_validator(validator["config"]["code"], inputs)
    assert result == {
        "valid": True,
        "errors": [],
        "missing_requirements": [],
        "invalid_scene_ids": [],
        "invalid_evidence_ids": [],
        "retry_from_step_id": "produce_video",
    }
    assert runner._evaluate_condition(
        quality_passed, {"quality_validation": result}
    )
    assert quality_passed["true_next"] == ["save_quality_ready"]
    assert quality_passed["false_next"] == ["save_revision_required"]

    mismatched = deepcopy(inputs)
    mismatched["quality_result"]["coverage"][0]["requirement"] = "Other claim"
    result = _execute_validator(validator["config"]["code"], mismatched)
    assert result["valid"] is False
    assert result["missing_requirements"] == ["Submit a request"]

    unknown_scene = deepcopy(inputs)
    unknown_scene["quality_result"]["coverage"][0]["scene_ids"] = [
        "scene-missing"
    ]
    result = _execute_validator(validator["config"]["code"], unknown_scene)
    assert result["valid"] is False
    assert result["invalid_scene_ids"] == ["scene-missing"]

    missing_artifact = deepcopy(inputs)
    missing_artifact["quality_result"]["coverage"][0][
        "evidence_artifact_ids"
    ] = ["artifact-missing"]
    result = _execute_validator(validator["config"]["code"], missing_artifact)
    assert result["valid"] is False
    assert result["invalid_evidence_ids"] == ["artifact-missing"]

    wrong_provenance = deepcopy(inputs)
    wrong_provenance["artifacts"][0]["scene_id"] = "scene-other"
    wrong_provenance["artifacts"][0]["provenance"]["scene_id"] = "scene-other"
    result = _execute_validator(validator["config"]["code"], wrong_provenance)
    assert result["valid"] is False
    assert result["invalid_evidence_ids"] == ["artifact-1"]

    two_scene_inputs = deepcopy(inputs)
    two_scene_inputs["plan"] = _two_scene_plan()
    two_scene_inputs["quality_result"]["coverage"][1]["scene_ids"] = ["scene-2"]
    two_scene_inputs["quality_result"]["coverage"][1][
        "evidence_artifact_ids"
    ] = ["artifact-2"]
    two_scene_inputs["artifacts"] = [
        _artifact_for_scene("scene-1", "artifact-1"),
        _artifact_for_scene("scene-2", "artifact-2"),
    ]
    result = _execute_validator(validator["config"]["code"], two_scene_inputs)
    assert result["valid"] is True

    reassigned = deepcopy(two_scene_inputs)
    reassigned["quality_result"]["coverage"][0]["scene_ids"] = ["scene-2"]
    reassigned["quality_result"]["coverage"][0][
        "evidence_artifact_ids"
    ] = ["artifact-2"]
    result = _execute_validator(validator["config"]["code"], reassigned)
    assert result["valid"] is False
    assert result["invalid_scene_ids"] == ["scene-2"]
    assert "qa_scene_mapping_mismatch" in {
        error["code"] for error in result["errors"]
    }

    extra_scene = deepcopy(two_scene_inputs)
    extra_scene["quality_result"]["coverage"][0]["scene_ids"] = [
        "scene-1",
        "scene-2",
    ]
    extra_scene["quality_result"]["coverage"][0][
        "evidence_artifact_ids"
    ] = ["artifact-1", "artifact-2"]
    result = _execute_validator(validator["config"]["code"], extra_scene)
    assert result["valid"] is False
    assert result["invalid_scene_ids"] == ["scene-2"]
    assert "qa_scene_mapping_mismatch" in {
        error["code"] for error in result["errors"]
    }

    failed_check = deepcopy(inputs)
    failed_check["quality_result"]["technical_checks"][0]["status"] = "fail"
    result = _execute_validator(validator["config"]["code"], failed_check)
    assert result["valid"] is False
    assert "technical_check_failed" in {error["code"] for error in result["errors"]}

    failed_probe = deepcopy(inputs)
    failed_probe["media_probe"] = {"status": "error", "error": "decode failed"}
    result = _execute_validator(validator["config"]["code"], failed_probe)
    assert result["valid"] is False
    assert "media_probe_failed" in {error["code"] for error in result["errors"]}

    undecodable = deepcopy(inputs)
    undecodable["media_probe"]["report"]["decodable"] = False
    result = _execute_validator(validator["config"]["code"], undecodable)
    assert result["valid"] is False
    assert "final_media_not_decodable" in {
        error["code"] for error in result["errors"]
    }

    failed_audio = deepcopy(inputs)
    failed_audio["audio_analysis"].update(
        {"status": "completed", "verdict": "fail", "non_empty": False}
    )
    result = _execute_validator(validator["config"]["code"], failed_audio)
    assert result["valid"] is False
    assert "audio_analysis_failed" in {error["code"] for error in result["errors"]}

    failed_subtitles = deepcopy(inputs)
    failed_subtitles["subtitle_analysis"]["verdict"] = "fail"
    result = _execute_validator(validator["config"]["code"], failed_subtitles)
    assert result["valid"] is False
    assert "subtitle_analysis_failed" in {
        error["code"] for error in result["errors"]
    }

    unreadable_style = deepcopy(inputs)
    unreadable_style["subtitle_analysis"]["style_evidence"]["Default"][
        "font_size"
    ] = 18
    result = _execute_validator(validator["config"]["code"], unreadable_style)
    assert result["valid"] is False
    assert "subtitle_style_failed" in {
        error["code"] for error in result["errors"]
    }

    invalid_style = deepcopy(inputs)
    invalid_style["subtitle_analysis"]["style_evidence"]["Default"].update(
        {"font_size": 52, "outline": 3, "shadow": 1, "bottom_safe": False}
    )
    result = _execute_validator(validator["config"]["code"], invalid_style)
    assert result["valid"] is False
    assert "subtitle_style_failed" in {
        error["code"] for error in result["errors"]
    }

    too_many_lines = deepcopy(inputs)
    too_many_lines["subtitle_analysis"]["maximum_line_count"] = 3
    result = _execute_validator(validator["config"]["code"], too_many_lines)
    assert result["valid"] is False
    assert "subtitle_line_limit_failed" in {
        error["code"] for error in result["errors"]
    }

    failed_frames = deepcopy(inputs)
    failed_frames["frame_evidence"] = {
        "status": "error",
        "sample_count": 0,
        "frames": [],
    }
    result = _execute_validator(validator["config"]["code"], failed_frames)
    assert result["valid"] is False
    assert "frame_evidence_failed" in {
        error["code"] for error in result["errors"]
    }

    uncovered_interval = deepcopy(inputs)
    uncovered_interval["frame_evidence"]["frames"][0]["timestamp_seconds"] = 9
    result = _execute_validator(validator["config"]["code"], uncovered_interval)
    assert result["valid"] is False
    assert "scene_frame_evidence_missing" in {
        error["code"] for error in result["errors"]
    }

    frame_backed_coverage = deepcopy(inputs)
    frame_backed_coverage["quality_result"]["coverage"][0][
        "evidence_artifact_ids"
    ].append("frame-document-1")
    result = _execute_validator(validator["config"]["code"], frame_backed_coverage)
    assert result["valid"] is True
    assert result["invalid_evidence_ids"] == []

    retry_state = _step(
        "create-product-video-v1", "save_revision_required"
    )["config"]["patch"]["retry_state"]
    assert retry_state["observed_problem"] == {
        "contract_validation": "{{quality_validation}}",
        "visual_findings": "{{quality_result.visual_findings}}",
    }


def test_repairable_quality_failure_retries_from_production():
    validator = _step("create-product-video-v1", "validate_quality_contract")
    inputs = {
        "request": _valid_request(),
        "plan": _valid_plan(),
        "quality_result": _valid_quality_result(),
        "artifacts": [_valid_artifact()],
        "timeline": _valid_production_result()["timeline"],
        "media_probe": _valid_media_probe(),
        "audio_analysis": _valid_audio_analysis(),
        "subtitle_analysis": _valid_subtitle_analysis(),
        "frame_evidence": _valid_frame_evidence(),
    }
    inputs["quality_result"]["verdict"] = "repairable_technical"

    result = _execute_validator(validator["config"]["code"], inputs)

    assert result["valid"] is False
    assert result["retry_from_step_id"] == "produce_video"
    retry_state = _step(
        "create-product-video-v1", "save_revision_required"
    )["config"]["patch"]["retry_state"]
    assert retry_state["step_id"] == "{{quality_validation.retry_from_step_id}}"
    assert retry_state["retry_from_step_id"] == (
        "{{quality_validation.retry_from_step_id}}"
    )


def test_product_video_materializes_visible_project_status_files():
    for workflow_slug in ("create-product-video-v1", "plan-product-video-v1"):
        steps = _steps_by_id(workflow_slug)

        assert steps["assign_project_storage"]["next"] == [
            "materialize_project_storage"
        ], workflow_slug
        materialize = steps["materialize_project_storage"]
        assert materialize["type"] == "tool"
        assert materialize["config"]["tool"] == "generate_file"
        assert materialize["config"]["args"]["name"] == (
            "{{project.state.project_root}}"
        )
        assert {
            item["path"]
            for item in materialize["config"]["args"]["params"]["files"]
        } == {"00-project-overview.md", "technical/run-state.json"}
        assert materialize["next"] == ["browser_preflight"]

        assert steps["save_discovery"]["next"] == ["project_discovery_report"]
        report = steps["project_discovery_report"]
        assert report["type"] == "tool"
        assert report["config"]["tool"] == "generate_file"
        report_files = {
            item["path"]: item["content"]
            for item in report["config"]["args"]["params"]["files"]
        }
        assert set(report_files) == {
            "00-project-overview.md",
            "technical/run-state.json",
            "technical/discovery-report.json",
        }
        assert report_files["technical/discovery-report.json"] == (
            "{{discovered_journey}}"
        )
        assert report["next"] == ["discovery_ready"]


def test_product_video_browser_handoff_surfaces_the_observed_blocker():
    from packages.core.ai.workflow_runner import _render_template

    schema_validator = Draft202012Validator(template.BROWSER_PREFLIGHT_SCHEMA)
    assert not list(schema_validator.iter_errors({
        "available": True,
        "requires_login": False,
        "blocker": None,
    }))
    for invalid_blocker in (None, "", "   "):
        assert list(schema_validator.iter_errors({
            "available": False,
            "requires_login": False,
            "blocker": invalid_blocker,
        }))

    for workflow_slug in ("create-product-video-v1", "plan-product-video-v1"):
        handoff = _step(workflow_slug, "browser_handoff")

        assert handoff["type"] == "wait"
        assert handoff["config"]["wait_type"] == "event"
        message = handoff["config"]["message"]
        assert "{{browser_preflight.blocker}}" in message
        rendered = _render_template(message, {
            "browser_preflight": {
                "blocker": "Login required {{untrusted_placeholder}}",
            },
        })
        assert rendered.startswith(
            "Browser readiness issue:\nLogin required {{untrusted_placeholder}}\n"
        )
        assert rendered.endswith("then resume planning.")


def test_production_graph_separates_invalid_blocked_and_retake_results():
    steps = _steps_by_id("create-product-video-v1")

    assert steps["produce_video"]["next"] == ["validate_production_contract"]
    assert steps["validate_production_contract"]["next"] == [
        "production_contract_valid"
    ]
    assert steps["production_contract_valid"]["config"]["expression"] == (
        "production_validation.valid == true"
    )
    assert steps["production_contract_valid"]["true_next"] == [
        "production_status_ready"
    ]
    assert steps["production_contract_valid"]["false_next"] == [
        "save_invalid_production_handoff"
    ]
    assert steps["production_status_ready"]["true_next"] == [
        "save_production_checkpoint"
    ]
    assert steps["production_status_ready"]["false_next"] == [
        "production_status_retake"
    ]
    assert steps["production_status_retake"]["true_next"] == [
        "save_production_handoff"
    ]
    assert steps["production_status_retake"]["false_next"] == [
        "save_blocked_production_handoff"
    ]
    assert steps["save_production_handoff"]["next"] == ["build_production_handoff"]
    assert steps["save_invalid_production_handoff"]["next"] == [
        "build_invalid_production_handoff"
    ]
    assert steps["build_invalid_production_handoff"]["next"] == ["needs_input"]
    assert steps["save_blocked_production_handoff"]["next"] == [
        "build_blocked_production_handoff"
    ]
    assert steps["build_blocked_production_handoff"]["next"] == ["needs_input"]

    blocked_retry = steps["save_blocked_production_handoff"]["config"]["patch"][
        "retry_state"
    ]
    assert blocked_retry["segment_ids"] == []
    assert blocked_retry["observed_problem"] == (
        "{{production_result.blocker.observed_problem}}"
    )
    assert blocked_retry["required_change"] == (
        "{{production_result.blocker.required_change}}"
    )
    assert blocked_retry["preserved_receipts"] == (
        "{{production_result.blocker.preserved_receipts}}"
    )
    assert blocked_retry["retry_from_step_id"] == "produce_video"


def test_revision_plan_preserves_unselected_scene_runtime_state():
    revise = next(
        spec for spec in template.WORKFLOW_SPECS
        if spec["slug"] == "revise-product-video-v1"
    )
    save_revision = next(
        step for step in revise["steps"]
        if step["id"] == "save_revision_plan"
    )["config"]

    assert save_revision["patch"]["plan"] == "{{revision_plan.updated_plan}}"
    assert "list_upserts" not in save_revision
    assert {
        "path": "scenes",
        "key": "scene_id",
        "keys": "{{revision_plan.updated_plan.scene_ids}}",
        "items": "{{revision_plan.selected_scenes}}",
    } in save_revision["list_reconciles"]
    assert {
        "path": "revision_history",
        "key": "revision_id",
        "items": ["{{revision_plan.revision_entry}}"],
    } in save_revision["list_appends"]


def test_revision_plan_preserves_ready_artifacts_for_selected_scenes():
    revise = next(
        spec for spec in template.WORKFLOW_SPECS
        if spec["slug"] == "revise-product-video-v1"
    )
    save_revision = next(
        step for step in revise["steps"]
        if step["id"] == "save_revision_plan"
    )["config"]

    assert "list_removes" not in save_revision

    collection_checkpoint = _step(
        "revise-product-video-v1",
        "save_collection_checkpoint",
    )["config"]
    assert "scenes" not in collection_checkpoint["patch"]
    assert "segments" not in collection_checkpoint["patch"]
    assert {
        "path": "segments",
        "key": "scene_id",
        "items": "{{effective_collection.segments}}",
    } in collection_checkpoint["list_appends"]


def test_revision_capture_reuses_ready_artifact_types_and_retries_only_unresolved_types():
    revise = next(
        spec for spec in template.WORKFLOW_SPECS
        if spec["slug"] == "revise-product-video-v1"
    )
    plan_revision = next(
        step for step in revise["steps"]
        if step["id"] == "plan_revision"
    )["config"]["input"]
    capture_scene = _step("revise-product-video-v1", "collect_assets")["config"]

    assert "preserve every ready artifact" in plan_revision
    assert "retry only missing or blocked artifact types" in plan_revision
    assert "Reuse existing ready artifact IDs" in capture_scene["input"]
    assert "only on the supplied Retry Segment IDs" in (
        capture_scene["forced_tool_calls"][0]["arguments"]["input"]
    )


def test_product_video_workflows_use_structured_agent_outputs():
    for spec in template.WORKFLOW_SPECS:
        for step in _executable_steps(spec["slug"]):
            if step["type"] != "agent":
                continue
            assert step["config"]["output_format"] == "json"
            assert isinstance(step["config"]["output_schema"], dict)


def test_product_video_capture_grants_allow_visual_computer_fallback():
    for workflow_slug in ("create-product-video-v1", "revise-product-video-v1"):
        grant = _step(workflow_slug, "grant_capture")

        assert "computer" in grant["config"]["scope"]["allowed_actions"]


def test_product_video_browser_agents_force_the_scoped_chrome_skill_first():
    browser_steps = {
        "browser_preflight",
        "explore_product",
        "collect_assets",
    }
    found = {}
    for spec in template.WORKFLOW_SPECS:
        for step in _executable_steps(spec["slug"]):
            if step["id"] in browser_steps:
                found[step["id"]] = step

    assert set(found) == browser_steps
    for step in found.values():
        forced = step["config"]["forced_tool_calls"]
        assert len(forced) == 1
        assert forced[0]["name"] == "invoke_skill"
        assert forced[0]["arguments"]["skill"] == "chrome"
        assert "Chrome" in step["config"]["input"]


def test_video_post_producer_skill_uses_responsive_subtitle_defaults():
    skill_text = (
        MARKETPLACE_SKILLS_DIR / "video-post-producer" / "SKILL.md"
    ).read_text(encoding="utf-8")
    default_call = skill_text.split("Unless the operator supplied a subtitle override, call:", 1)[
        1
    ].split("```", 2)[1]

    assert 'timeline_path="<project_root>/timeline/timeline.json"' in default_call
    assert "max_lines=2" in default_call
    assert "style=" not in default_call
    assert '"font_size":20' not in default_call
    assert '"margin_v":38' not in default_call
    assert "clamp(round(short_edge * 14 / 288), 16, 56)" in skill_text
    assert '"font_size":24' not in skill_text


def test_video_post_producer_skill_enforces_measured_scene_retake_contract():
    skill_text = (
        MARKETPLACE_SKILLS_DIR / "video-post-producer" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "visual_scene_intervals" in skill_text
    assert "similarity >= 0.90" in skill_text
    assert "coverage >= 0.95" in skill_text
    assert "missing_scene_ids" in skill_text
    assert "deficient_scene_ids" in skill_text
    assert "retry_segment_ids" in skill_text
    assert "deficit_evidence" in skill_text
    assert "min(0.75s, 10% of required_narration_span_seconds)" in skill_text
    assert "Never time-stretch unrelated footage" in skill_text
    assert "narration/audio" in skill_text
    assert "subtitles/subtitle" in skill_text
    assert "final_video/video" in skill_text
    assert "directly from `visual_scene_intervals`" in skill_text
    assert "timing_sources" in skill_text
    assert "scene_coverage" in skill_text
    assert "transcription_model" in skill_text
    assert "blocker_type" in skill_text
    assert "blocked result keeps deficient_scene_ids, retry_segment_ids, and deficit_evidence empty" in skill_text


def test_product_video_installed_defaults_are_generic():
    serialized = json.dumps(
        {
            "template": {
                "key": template.ProductVideoStudioTemplate.key,
                "title": template.ProductVideoStudioTemplate.title,
            },
            "roles": [role.__dict__ for role in template.ROLE_SPECS],
            "workflows": template.WORKFLOW_SPECS,
        },
        ensure_ascii=False,
    ).lower()

    assert "stickman" not in serialized
    assert "localhost:3010" not in serialized
    assert "product demo" not in serialized
    assert "product-demo" not in serialized
    assert "capture_authorized" not in serialized
    assert "test_account" not in serialized


def test_initial_project_has_no_product_specific_defaults():
    project = template.initial_product_video_project(_valid_request())

    assert project == {
        "request": _valid_request(),
        "project_root": None,
        "ledger_path": None,
        "artifact_manifest_path": None,
        "current_phase": "request",
        "business_outcome": "in_progress",
        "product_source": {
            "start_url": "https://app.example.test",
            "browser_session": "current_paired_chrome_session",
        },
        "output_profile": _valid_request()["output_profile"],
        "discovered_journey": None,
        "plan": None,
        "approved_plan_version": None,
        "capture_grant_id": None,
        "scenes": [],
        "segments": [],
        "artifacts": [],
        "checkpoints": {},
        "retry_state": None,
        "final_artifacts": [],
        "history": [],
        "timeline": None,
        "quality_result": None,
        "operator_acceptance": None,
        "revision_history": [],
        "blockers": [],
    }


def test_frozen_product_video_blueprint_matches_recipe_and_has_no_presets():
    payload = json.loads(BLUEPRINT_PATH.read_text(encoding="utf-8"))
    validate_payload(payload)

    assert payload["manifest"]["slug"] == "product-video-studio-v1"
    assert payload["manifest"]["title"] == "Product Video Studio"
    assert payload["embedded"]["knowledge_packs"] == []
    assert payload["recipe"]["starter_tasks"] == []
    assert payload["recipe"]["scheduled_jobs"] == []
    assert payload["recipe"]["goals"] == []
    assert [workflow["slug"] for workflow in payload["recipe"]["workflows"]] == (
        template.WORKFLOW_SLUGS
    )
    assert [
        workflow["slug"]
        for workflow in payload["recipe"]["workflows"]
        if workflow["binding_config"]["chat_entrypoint"]["enabled"]
    ] == template.USER_WORKFLOW_SLUGS
    assert [
        workflow["slug"]
        for workflow in payload["recipe"]["workflows"]
        if workflow["internal"]
    ] == template.INTERNAL_WORKFLOW_SLUGS

    for frozen, spec in zip(payload["recipe"]["workflows"], template.WORKFLOW_SPECS):
        assert frozen["steps"] == spec["steps"]
        assert frozen["variables"] == [
            {"key": key, "default": value}
            for key, value in spec["variables"].items()
        ]

    serialized = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in (
        "stickman",
        "localhost:3010",
        "product demo video studio",
        "product-demo-video-studio",
        "capture_authorized",
        "test_account",
        "the 5-minute rule",
    ):
        assert forbidden not in serialized


def test_frozen_product_video_blueprint_binds_media_qa_tools():
    payload = json.loads(BLUEPRINT_PATH.read_text(encoding="utf-8"))
    declared_tools = set(payload["contract"]["requires"]["tools"])
    assert {
        "probe_media",
        "render_frame_samples",
        "analyze_audio",
        "validate_subtitles",
        "still_to_video",
    } <= declared_tools
    for agent in payload["embedded"]["agents"]:
        assert set(agent["tool_bindings"]) <= declared_tools


def test_frozen_product_video_blueprint_embeds_full_marketplace_skill_contracts():
    payload = json.loads(BLUEPRINT_PATH.read_text(encoding="utf-8"))
    embedded = {skill["slug"]: skill for skill in payload["embedded"]["skills"]}

    for slug in template.RECOMMENDED_SKILLS:
        skill_md = (
            MARKETPLACE_SKILLS_DIR / slug.replace("_", "-") / "SKILL.md"
        ).read_text(encoding="utf-8")
        source = _parse_skill_frontmatter(skill_md)

        assert embedded[slug]["system_prompt"] == source["body"]
        assert embedded[slug]["tools"] == source["tools"]


@pytest.mark.parametrize(
    "skill_slug",
    [
        "script-storyboard-planner",
        "screen-asset-collector",
        "video-post-producer",
        "video-quality-reviewer",
    ],
)
def test_product_video_skills_use_workflow_project_as_the_state_authority(skill_slug):
    prompt = (MARKETPLACE_SKILLS_DIR / skill_slug / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "WorkflowProject state is authoritative" in prompt
    assert "files are projections" in prompt
    assert "ledger as authoritative" not in prompt.lower()
