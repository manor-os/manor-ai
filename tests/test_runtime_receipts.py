"""Host receipt ledger: effect states and per-tool receipt projections."""

from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.receipts import (
    RECEIPT_PROJECTIONS,
    EffectState,
    runtime_aggregate_effect_state,
    runtime_project_receipts,
    runtime_tool_effect_state,
)
from packages.core.ai.runtime.tool_results import (
    RuntimeNoEffectToolResult,
    RuntimePreflightValidationError,
)


def test_successful_action_is_confirmed():
    result = json.dumps({"created": True, "document": {"document_id": "d1"}})
    assert runtime_tool_effect_state("generate_file", {"kind": "document"}, result) is EffectState.CONFIRMED


def test_failed_action_is_unconfirmed():
    result = json.dumps({"status": "error", "error": "provider_timeout"})
    assert runtime_tool_effect_state("generate_file", {"kind": "document"}, result) is EffectState.UNCONFIRMED


def test_read_only_tool_never_has_an_effect():
    ok = json.dumps({"content": "hello"})
    failed = json.dumps({"status": "error", "error": "not_found"})
    assert runtime_tool_effect_state("read_file", {"path": "a.md"}, ok) is EffectState.NONE
    assert runtime_tool_effect_state("read_file", {"path": "a.md"}, failed) is EffectState.NONE


def test_host_proofs_of_no_mutation_are_none():
    preflight = RuntimePreflightValidationError(json.dumps({"status": "error", "error": "bad_args"}))
    no_effect = RuntimeNoEffectToolResult.patch_target_not_found(
        json.dumps({"status": "error", "error": "target_not_found"})
    )
    assert runtime_tool_effect_state("generate_file", {"kind": "document"}, preflight) is EffectState.NONE
    assert runtime_tool_effect_state("patch_file", {"path": "a.md"}, no_effect) is EffectState.NONE


def test_unclassified_tool_error_is_unconfirmed():
    failed = json.dumps({"status": "error", "error": "boom"})
    assert runtime_tool_effect_state("mcp__vendor__do_thing", {}, failed) is EffectState.UNCONFIRMED


def test_json_cannot_claim_no_effect():
    # Only host carrier types prove "no mutation"; a tool's own JSON cannot.
    failed = json.dumps({"status": "error", "preflight_validation": True, "no_effect": True})
    assert runtime_tool_effect_state("generate_file", {"kind": "document"}, failed) is EffectState.UNCONFIRMED


def _p(tool, args, payload):
    return runtime_project_receipts(tool, args, payload)


def test_derived_poster_is_the_file_receipt_only_when_ready():
    poster = {"document_id": "poster-doc", "fs_path": "images/cover.png", "filename": "cover.png", "sha256": "c" * 64}
    assert _p("derive_image_aspect", {}, {"ready": True, "derived": True, "poster": poster}) == [{
        "kind": "file", "operation": "derive", "document_id": "poster-doc", "fs_path": "images/cover.png",
        "name": "cover.png", "sha256": "c" * 64,
    }]
    assert _p("derive_image_aspect", {}, {"ready": False, "blocker": "no source", "poster": None}) == []


def test_generate_file_document_projection():
    payload = {"created": True, "document": {
        "document_id": "doc1", "fs_path": "Workspaces/a/report.docx",
        "source_sha256": "abc", "viewer_url": "/viewer/doc1", "name": "report.docx",
    }}
    assert _p("generate_file", {"kind": "document"}, payload) == [{
        "kind": "file", "operation": "document", "document_id": "doc1",
        "fs_path": "Workspaces/a/report.docx", "sha256": "abc",
        "url": "/viewer/doc1", "name": "report.docx",
    }]


def test_generate_file_media_projection():
    image = {"image_url": "/api/v1/fs/e/x.png", "fs_path": "x.png"}
    audio = {"kind": "audio", "status": "completed", "audio_url": "/api/v1/fs/e/v.mp3", "fs_path": "v.mp3"}
    assert _p("generate_file", {"kind": "image"}, image) == [{
        "kind": "file", "operation": "image", "fs_path": "x.png", "url": "/api/v1/fs/e/x.png",
    }]
    assert _p("generate_file", {"kind": "audio"}, audio) == [{
        "kind": "file", "operation": "audio", "fs_path": "v.mp3", "url": "/api/v1/fs/e/v.mp3",
    }]


def test_generate_file_pending_video_has_no_receipt_until_waited():
    pending = {"kind": "video", "status": "pending", "job_id": "j1"}
    waited = {"jobs": [
        {"job_id": "j1", "status": "completed", "fs_path": "v.mp4", "document_id": "d9", "result_url": "/api/v1/fs/e/v.mp4"},
        {"job_id": "j2", "status": "failed"},
    ]}
    assert _p("generate_file", {"kind": "video"}, pending) == []
    assert _p("wait_media_jobs", {}, waited) == [{
        "kind": "file", "operation": "media_job", "document_id": "d9",
        "fs_path": "v.mp4", "url": "/api/v1/fs/e/v.mp4", "external_id": "j1",
    }]


def test_import_free_music_projection():
    payload = {"status": "completed", "document": {"document_id": "m1", "id": "m1", "fs_path": "music/a.mp3", "name": "a.mp3"}, "track": {}}
    assert _p("import_free_music", {}, payload) == [{
        "kind": "file", "operation": "import", "document_id": "m1",
        "fs_path": "music/a.mp3", "name": "a.mp3",
    }]


def test_video_edit_review_and_render_projection():
    review = {
        "status": "ready_for_review", "session_id": "s1", "project_path": "proj",
        "review_sha256": "rev", "review_artifacts": [
            {"document_id": "f1", "fs_path": "proj/snap1.png", "url": "/api/v1/fs/e/proj/snap1.png"},
        ],
    }
    render = {
        "status": "completed", "session_id": "s1", "project_path": "proj", "source_sha256": "src",
        "video": {"document_id": "v1", "fs_path": "proj/out.mp4", "url": "/api/v1/fs/e/proj/out.mp4", "name": "out.mp4"},
    }
    assert _p("video_edit", {"operation": "review"}, review) == [
        {"kind": "review", "operation": "review", "sha256": "rev", "external_id": "s1", "fs_path": "proj"},
        {"kind": "file", "operation": "review_artifact", "document_id": "f1",
         "fs_path": "proj/snap1.png", "url": "/api/v1/fs/e/proj/snap1.png"},
    ]
    assert _p("video_edit", {"operation": "render"}, render) == [{
        "kind": "file", "operation": "render", "document_id": "v1", "fs_path": "proj/out.mp4",
        "url": "/api/v1/fs/e/proj/out.mp4", "name": "out.mp4", "sha256": "src", "external_id": "s1",
    }]
    # A failed review returns status=completed with review_passed=False: no receipt.
    assert _p("video_edit", {"operation": "review"}, {"status": "completed", "review_passed": False}) == []


def test_publish_site_projection():
    payload = {"published": True, "url": "https://x.example", "site_id": "site1", "revision": 3}
    assert _p("publish_site", {}, payload) == [{
        "kind": "publication", "operation": "publish", "url": "https://x.example",
        "external_id": "site1", "revision": 3,
    }]


def test_unregistered_tool_has_no_receipt():
    assert _p("read_file", {}, {"content": "x"}) == []
    assert "read_file" not in RECEIPT_PROJECTIONS


# --- effect_state: CONFIRMED requires a positive success -----------------------


@pytest.mark.parametrize("status", ["blocked", "failed", "timeout"])
def test_tool_reported_refusal_or_failure_is_unconfirmed(status):
    # A tool's own "blocked" refusal (e.g. generate_file video bad duration,
    # import_free_music _json_blocked) is not proof that nothing happened.
    result = json.dumps({"status": status, "error": "target_duration_seconds must be a whole number"})
    assert runtime_tool_effect_state("generate_file", {"kind": "video"}, result) is EffectState.UNCONFIRMED


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "approval_required", "approval_id": "a1"},
        {"status": "waiting_human"},
        {"status": "handoff_required", "reason": "chrome_confirmation_handoff_required"},
        {"status": "rejected", "reason": "user declined"},
        {"status": "cancelled"},
        {"status": "canceled"},
        {"status": "hitl_required"},
        {"status": "blocked"},
        {"approval_required": True, "message": "needs approval"},
        {"status": "blocked", "approval_required": True},
        {"__hitl__": True, "hitl": {"id": "h1", "type": "approval"}},
        {"control": {"kind": "approval_required", "blocking_result": {}}},
    ],
)
@pytest.mark.parametrize("tool", ["generate_file", "mcp__vendor__do", "invoke_skill"])
def test_gate_shaped_json_never_proves_no_effect(tool, payload):
    # Plain JSON can be produced by any tool (or relayed from a child skill whose
    # earlier steps already wrote files): it must never reach NONE.
    args = {"kind": "document"} if tool == "generate_file" else {}
    assert runtime_tool_effect_state(tool, args, json.dumps(payload)) is EffectState.UNCONFIRMED


def test_incidental_approval_flag_on_a_terminal_status_stays_confirmed():
    # video_edit review registers frame documents, then asks for review.
    review = {"status": "ready_for_review", "approval_required": True, "review_sha256": "r"}
    assert runtime_tool_effect_state("video_edit", {"operation": "review"}, json.dumps(review)) is EffectState.CONFIRMED
    done = {"status": "completed", "approval_required": True}
    assert runtime_tool_effect_state("mcp__vendor__do", {}, json.dumps(done)) is EffectState.CONFIRMED


def test_host_gate_carrier_proves_no_effect():
    from packages.core.ai.runtime.tool_results import RuntimeGateToolResult

    gate = RuntimeGateToolResult(json.dumps({"error": "approval_required", "approval_token": "t"}))
    assert runtime_tool_effect_state("generate_file", {"kind": "document"}, gate) is EffectState.NONE
    assert runtime_tool_effect_state("mcp__vendor__do", {}, RuntimeGateToolResult("{}")) is EffectState.NONE


def test_suspended_call_is_not_proof_of_no_effect():
    # Composite tools (chrome_sequence, coordinator waiting_child, invoke_skill)
    # suspend after real work, so a suspension is unfinished, not effect-free.
    from packages.core.ai.runtime.control import RuntimeToolSuspension

    suspension = RuntimeToolSuspension(
        kind="waiting_human", reservation_id="r1", poll_after_seconds=5, deadline_at="2030-01-01T00:00:00Z",
    )
    assert runtime_tool_effect_state("invoke_skill", {}, suspension) is EffectState.UNCONFIRMED
    assert runtime_tool_effect_state("mcp__vendor__do", {}, suspension) is EffectState.UNCONFIRMED


# --- host gate mint site: RuntimeApprovalDecision.block ----------------------------


def _gate_request():
    from packages.core.ai.runtime.approvals import RuntimeApprovalRequest

    return RuntimeApprovalRequest(
        tool_name="workspace_create_task",
        arguments={"title": "Review"},
        entity_id="ent_1",
        user_id="user_1",
        workspace_id="ws_1",
        conversation_id="conv_1",
    )


def _gate_adapter(blocked):
    from packages.core.ai.runtime.approvals import RuntimeApprovalAction
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolClassification

    class Adapter:
        def classify_request(self, request):
            return RuntimeToolClassification.action_call(
                RuntimeApprovalAction(
                    kind="action",
                    action_key="workspace.task.create",
                    risk_level="medium",
                    title="create workspace task",
                )
            )

        async def guard_request(self, request):
            return blocked

    return Adapter()


async def test_approval_decision_block_mints_the_host_gate_carrier():
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.runtime.tool_results import RuntimeGateToolResult

    text = '{"error":"approval_required","approval_token":"tok_1"}'
    middleware = RuntimeApprovalMiddleware(policy_adapter=_gate_adapter(text))
    decision = await middleware.guard_request(_gate_request())
    assert not decision.allowed
    assert isinstance(decision.blocked_result, RuntimeGateToolResult)
    assert decision.blocked_result == text  # still the same str
    assert runtime_tool_effect_state("workspace_create_task", {"title": "Review"}, decision.blocked_result) is EffectState.NONE

    direct = RuntimeApprovalDecision.block("workspace_create_task", text, decision.classification)
    assert isinstance(direct.blocked_result, RuntimeGateToolResult)
    assert direct.event is not None and direct.event.type == "approval_required"

    allowed = await RuntimeApprovalMiddleware(policy_adapter=_gate_adapter(None)).guard_request(_gate_request())
    assert allowed.allowed and allowed.blocked_result is None


async def test_guard_tool_action_returns_the_host_gate_carrier():
    from packages.core.ai.runtime.approvals import RuntimeApprovalMiddleware
    from packages.core.ai.runtime.tool_results import RuntimeGateToolResult

    middleware = RuntimeApprovalMiddleware(policy_adapter=_gate_adapter('{"error":"approval_required"}'))
    blocked = await middleware.guard_tool_action(
        tool_name="workspace_create_task", arguments={"title": "Review"}, entity_id="ent_1",
        user_id="user_1", workspace_id="ws_1", conversation_id="conv_1",
    )
    assert isinstance(blocked, RuntimeGateToolResult)


@pytest.mark.parametrize("status", ["unknown", "partial", "interrupted"])
def test_indeterminate_status_is_unconfirmed(status):
    result = json.dumps({"status": status})
    assert runtime_tool_effect_state("generate_file", {"kind": "video"}, result) is EffectState.UNCONFIRMED


def test_accepted_async_job_is_confirmed():
    result = json.dumps({"kind": "video", "status": "pending", "job_id": "j1"})
    assert runtime_tool_effect_state("generate_file", {"kind": "video"}, result) is EffectState.CONFIRMED


# --- aggregate -------------------------------------------------------------------


def test_aggregate_effect_state_ordering_and_bare_string():
    assert runtime_aggregate_effect_state(["none", "confirmed"]) is EffectState.CONFIRMED
    assert runtime_aggregate_effect_state(["confirmed", "unconfirmed", "none"]) is EffectState.UNCONFIRMED
    assert runtime_aggregate_effect_state([]) is EffectState.NONE
    assert runtime_aggregate_effect_state(None) is EffectState.NONE
    assert runtime_aggregate_effect_state(["bogus"]) is EffectState.UNCONFIRMED
    # A bare str is one state, not an iterable of characters.
    assert runtime_aggregate_effect_state("confirmed") is EffectState.CONFIRMED
    assert runtime_aggregate_effect_state("none") is EffectState.NONE
    assert runtime_aggregate_effect_state(EffectState.UNCONFIRMED) is EffectState.UNCONFIRMED


# --- projections: no skeleton receipts --------------------------------------------


def test_video_edit_review_without_sha_has_no_review_receipt_but_keeps_artifacts():
    review = {
        "status": "ready_for_review", "session_id": "s1", "project_path": "proj",
        "review_artifacts": [{"document_id": "f1", "fs_path": "proj/snap1.png"}],
    }
    assert _p("video_edit", {"operation": "review"}, review) == [
        {"kind": "file", "operation": "review_artifact", "document_id": "f1", "fs_path": "proj/snap1.png"},
    ]


def test_publish_site_without_url_or_site_id_has_no_receipt():
    assert _p("publish_site", {}, {"published": True, "revision": 2}) == []
    assert _p("publish_site", {}, {"published": True, "url": "  ", "site_id": ""}) == []
    assert _p("publish_site", {}, {"published": True, "site_id": "s1"}) == [
        {"kind": "publication", "operation": "publish", "external_id": "s1"},
    ]


def test_generate_file_provider_only_image_is_not_a_stored_artifact():
    transient = {"image_url": "https://provider.example/x.png", "saved_to_knowledge": False}
    stored = {"image_url": "/api/v1/fs/e/x.png", "fs_path": "x.png", "saved_to_knowledge": False}
    assert _p("generate_file", {"kind": "image"}, transient) == []
    assert _p("generate_file", {"kind": "image"}, stored) == [{
        "kind": "file", "operation": "image", "fs_path": "x.png", "url": "/api/v1/fs/e/x.png",
    }]


def test_generate_file_code_bundle_projects_its_real_files_list():
    payload = {
        "created": True, "kind": "code", "bundle_path": "app", "entry": "app/index.html",
        "entry_url": "/api/v1/fs/e/app/index.html",
        "files": [
            {
                "path": "app/index.html", "url": "/api/v1/fs/e/app/index.html", "size": 10,
                "source_sha256": "h1", "mtime_ns": 1, "knowledge_synced": True,
                "document_id": "d1", "viewer_url": "/viewer/d1", "knowledge_sync_reason": "ok",
            },
        ],
    }
    assert _p("generate_file", {"kind": "code"}, payload) == [{
        "kind": "file", "operation": "code", "document_id": "d1", "fs_path": "app/index.html",
        "sha256": "h1", "url": "/api/v1/fs/e/app/index.html",
    }]


def test_id_and_path_fallback_keys():
    payload = {"files": [{"id": "i1", "path": "p.txt"}]}
    assert _p("generate_file", {"kind": "code"}, payload) == [{
        "kind": "file", "operation": "code", "document_id": "i1", "fs_path": "p.txt",
    }]


def test_non_dict_list_items_are_skipped():
    files = {"files": ["junk", None, 3, {"path": "p.txt"}]}
    jobs = {"jobs": ["junk", None, {"job_id": "j", "status": "completed", "fs_path": "v.mp4"}]}
    review = {"status": "ready_for_review", "review_sha256": "r", "review_artifacts": ["junk", None]}
    assert _p("generate_file", {"kind": "code"}, files) == [
        {"kind": "file", "operation": "code", "fs_path": "p.txt"},
    ]
    assert _p("wait_media_jobs", {}, jobs) == [
        {"kind": "file", "operation": "media_job", "fs_path": "v.mp4", "external_id": "j"},
    ]
    assert _p("video_edit", {"operation": "review"}, review) == [
        {"kind": "review", "operation": "review", "sha256": "r"},
    ]


# --- generate_file code-bundle assets ---------------------------------------------


def test_generate_file_code_bundle_projects_imported_assets_at_entity_paths():
    # assets[].path is bundle-relative (PlannedFile.rel), files[].path is entity-relative.
    payload = {
        "created": True, "kind": "code", "bundle_path": "Workspaces/w/app", "entry": "Workspaces/w/app/index.html",
        "files": [{"path": "Workspaces/w/app/index.html", "source_sha256": "h1", "document_id": "d1"}],
        "assets": [
            {"path": "img/logo.png", "size": 5, "sha256": "a1", "created": True,
             "document_id": "d2", "copied_from": "uploads/logo.png"},
            {"path": "img/bg.png", "sha256": "a2", "created": False, "document_id": None},
            "junk",
        ],
    }
    assert _p("generate_file", {"kind": "code"}, payload) == [
        {"kind": "file", "operation": "code", "document_id": "d1",
         "fs_path": "Workspaces/w/app/index.html", "sha256": "h1"},
        {"kind": "file", "operation": "code_asset", "document_id": "d2",
         "fs_path": "Workspaces/w/app/img/logo.png", "sha256": "a1"},
        {"kind": "file", "operation": "code_asset", "fs_path": "Workspaces/w/app/img/bg.png", "sha256": "a2"},
    ]


def test_generate_file_code_asset_without_bundle_path_keeps_only_document_id():
    payload = {"files": [], "assets": [{"path": "img/logo.png", "sha256": "a1", "document_id": "d2"}]}
    assert _p("generate_file", {"kind": "code"}, payload) == [
        {"kind": "file", "operation": "code_asset", "document_id": "d2", "sha256": "a1"},
    ]


# --- media file tools --------------------------------------------------------------


def test_generate_image_projection_matches_generate_file_image():
    stored = {"image_url": "/api/v1/fs/e/images/x.png", "fs_path": "images/x.png", "saved_to_knowledge": True,
              "workspace_path": "images/x.png", "prompt": "p", "size": "1024x1024", "model": "m"}
    group = {"document_id": "g1", "saved_to_knowledge": True, "pending_group_share": True}
    transient = {"image_url": "https://provider.example/x.png", "saved_to_knowledge": False}
    assert _p("generate_image", {"prompt": "p"}, stored) == [{
        "kind": "file", "operation": "image", "fs_path": "images/x.png", "url": "/api/v1/fs/e/images/x.png",
    }]
    assert _p("generate_image", {}, group) == [{"kind": "file", "operation": "image", "document_id": "g1"}]
    assert _p("generate_image", {}, transient) == []


@pytest.mark.parametrize(
    ("tool", "operation", "kind"),
    [
        ("merge_videos", "merge", "video"),
        ("still_to_video", "render", "video"),
        ("normalize_audio_loudness", "normalize", "audio"),
        ("align_subtitles", "subtitle", "subtitle"),
    ],
)
def test_completed_media_file_projection(tool, operation, kind):
    payload = {
        "kind": kind, "status": "completed", "document_id": "d1", "name": "out.bin",
        "result_url": "/api/v1/fs/e/a/out.bin", "fs_path": "a/out.bin", "file_size": 10,
    }
    assert _p(tool, {}, payload) == [{
        "kind": "file", "operation": operation, "document_id": "d1", "fs_path": "a/out.bin",
        "url": "/api/v1/fs/e/a/out.bin", "name": "out.bin",
    }]
    assert _p(tool, {}, {"status": "error", "code": "x", "error": "boom"}) == []
    assert _p(tool, {}, {"status": "blocked", "code": "x", "error": "no", "fs_path": "a/out.bin"}) == []


def test_compose_video_timeline_projects_video_and_editor_recipe():
    payload = {
        "kind": "video", "status": "completed", "document_id": "v1", "name": "final.mp4",
        "result_url": "/api/v1/fs/e/a/final.mp4", "fs_path": "a/final.mp4",
        "editor_recipe": {
            "document_id": "r1", "name": "final.manor-edit.json", "fs_path": "a/final.manor-edit.json",
            "result_url": "/api/v1/fs/e/a/final.manor-edit.json", "abs_path": "/srv/x", "kind": "manor.video_edit_recipe",
        },
        "editor_recipe_document_id": "r1", "editor_recipe_path": "a/final.manor-edit.json",
    }
    assert _p("compose_video_timeline", {}, payload) == [
        {"kind": "file", "operation": "compose", "document_id": "v1", "fs_path": "a/final.mp4",
         "url": "/api/v1/fs/e/a/final.mp4", "name": "final.mp4"},
        {"kind": "file", "operation": "editor_recipe", "document_id": "r1", "fs_path": "a/final.manor-edit.json",
         "url": "/api/v1/fs/e/a/final.manor-edit.json", "name": "final.manor-edit.json"},
    ]
    # compose_incomplete: the video is saved but delivery failed -> UNCONFIRMED, and no receipts.
    partial = {"status": "partial", "error": "compose_incomplete", "execution": {"saved_outputs": [{"fs_path": "a/final.mp4"}]}}
    assert _p("compose_video_timeline", {}, partial) == []


@pytest.mark.parametrize("tool", ["build_narration_timeline", "prepare_narration_timeline"])
def test_narration_timeline_projects_its_three_json_artifacts(tool):
    payload = {
        "kind": "narration_timeline", "status": "completed", "audio_tracks": [{"path": "voice/1.wav"}],
        "manifest": {"document_id": "m1", "fs_path": "a/narration-manifest.json", "name": "narration-manifest.json"},
        "timeline": {"document_id": "t1", "fs_path": "a/narration-timeline.json", "name": "narration-timeline.json"},
        "cues": {"document_id": "c1", "fs_path": "a/subtitle-cues.json", "name": "subtitle-cues.json"},
        "normalized_audio_paths": ["a/n/segment-001.wav"],
    }
    assert _p(tool, {}, payload) == [
        {"kind": "file", "operation": "narration_manifest", "document_id": "m1",
         "fs_path": "a/narration-manifest.json", "name": "narration-manifest.json"},
        {"kind": "file", "operation": "narration_timeline", "document_id": "t1",
         "fs_path": "a/narration-timeline.json", "name": "narration-timeline.json"},
        {"kind": "file", "operation": "subtitle_cues", "document_id": "c1",
         "fs_path": "a/subtitle-cues.json", "name": "subtitle-cues.json"},
    ]
    repair = {"status": "repair_required", "code": "narration_source_receipt_missing", "repair_segments": []}
    assert _p(tool, {}, repair) == []


def test_render_frame_samples_projects_each_frame():
    payload = {"status": "completed", "input_path": "a/v.mp4", "sample_count": 1, "frames": [
        {"index": 1, "timestamp_seconds": 2, "document_id": "f1", "name": "frame-01.png",
         "fs_path": "a/preview/frame-01.png", "result_url": "/api/v1/fs/e/a/preview/frame-01.png"},
    ]}
    assert _p("render_frame_samples", {}, payload) == [{
        "kind": "file", "operation": "frame_sample", "document_id": "f1", "fs_path": "a/preview/frame-01.png",
        "url": "/api/v1/fs/e/a/preview/frame-01.png", "name": "frame-01.png",
    }]
    failed = {"status": "error", "code": "render_frame_samples_failed", "completed_frames": payload["frames"]}
    assert _p("render_frame_samples", {}, failed) == []


# --- recording + creative studio ---------------------------------------------------


@pytest.mark.parametrize("tool", ["prepare_recording", "prepare_creative_studio"])
def test_prepare_tools_project_their_frozen_snapshot(tool):
    payload = {
        "status": "completed", "duration_seconds": 30,
        "snapshot": {"document_id": "s1", "fs_path": "w/out/01/source.json", "file_size": 9,
                     "viewer_url": "/api/v1/fs/e/w/out/01/source.json", "sha256": "h1"},
        "sources": [{"fs_path": "uploads/in.mp4", "sha256": "src"}],
    }
    assert _p(tool, {}, payload) == [{
        "kind": "file", "operation": "snapshot", "document_id": "s1", "fs_path": "w/out/01/source.json",
        "sha256": "h1", "url": "/api/v1/fs/e/w/out/01/source.json",
    }]
    assert _p(tool, {}, {"status": "failed", "error": "boom"}) == []
    assert _p(tool, {}, {"status": "error", "code": "recording_preparation_failed", "error": "x"}) == []


def test_render_recording_projects_every_output_and_the_delivery_receipt():
    saved = lambda doc, path: {"document_id": doc, "fs_path": path, "file_size": 1,  # noqa: E731
                               "viewer_url": f"/api/v1/fs/e/{path}", "sha256": f"sha-{doc}"}
    video = {
        "kind": "video", "status": "completed", "document_id": "v1", "name": "final.mp4",
        "result_url": "/api/v1/fs/e/o/01/final.mp4", "fs_path": "o/01/final.mp4",
        "editor_recipe": {"document_id": "r1", "fs_path": "o/01/final.manor-edit.json", "name": "final.manor-edit.json"},
    }
    payload = {
        "status": "completed", "machine_pass": True, "mode": "tutorial",
        "final_videos": [{**video, "name": "Edited tutorial.mp4"}],
        "outputs": [{
            "title": "Edited tutorial", "video": video, "sha256": "final-sha",
            "subtitles": saved("s1", "o/01/captions.srt"), "burned_subtitles": saved("a1", "o/01/captions.ass"),
            "timeline": saved("t1", "o/01/timeline.json"),
            "preview": {"status": "completed", "frames": [
                {"document_id": "f1", "fs_path": "o/01/preview/frame-01.png", "name": "frame-01.png"},
            ]},
        }],
        "receipt": saved("d1", "o/delivery.json"),
    }
    assert _p("render_recording", {"mode": "tutorial"}, payload) == [
        {"kind": "file", "operation": "render", "document_id": "v1", "fs_path": "o/01/final.mp4",
         "url": "/api/v1/fs/e/o/01/final.mp4", "name": "final.mp4", "sha256": "final-sha"},
        {"kind": "file", "operation": "editor_recipe", "document_id": "r1",
         "fs_path": "o/01/final.manor-edit.json", "name": "final.manor-edit.json"},
        {"kind": "file", "operation": "subtitle", "document_id": "s1", "fs_path": "o/01/captions.srt",
         "url": "/api/v1/fs/e/o/01/captions.srt", "sha256": "sha-s1"},
        {"kind": "file", "operation": "burned_subtitle", "document_id": "a1", "fs_path": "o/01/captions.ass",
         "url": "/api/v1/fs/e/o/01/captions.ass", "sha256": "sha-a1"},
        {"kind": "file", "operation": "timeline", "document_id": "t1", "fs_path": "o/01/timeline.json",
         "url": "/api/v1/fs/e/o/01/timeline.json", "sha256": "sha-t1"},
        {"kind": "file", "operation": "frame_sample", "document_id": "f1",
         "fs_path": "o/01/preview/frame-01.png", "name": "frame-01.png"},
        {"kind": "file", "operation": "delivery_receipt", "document_id": "d1", "fs_path": "o/delivery.json",
         "url": "/api/v1/fs/e/o/delivery.json", "sha256": "sha-d1"},
    ]
    failed = {"status": "failed", "code": "recording_render_failed", "error": "x",
              "completed_outputs": payload["outputs"]}
    assert _p("render_recording", {}, failed) == []


@pytest.mark.parametrize(("tool", "operation"), [
    ("compose_creative_publication", "publication"), ("compose_creative_deck", "deck"), ("compose_creative_dub", "dub"),
    ("compose_creative_video", "video"),
])
def test_creative_compose_tools_project_their_returned_paths(tool, operation):
    payload = {"status": "completed", "files": ["w/o/publication.pptx", "w/o/publication.pdf", " ", 3, "w/o/notes.md"]}
    assert _p(tool, {}, payload) == [
        {"kind": "file", "operation": operation, "fs_path": "w/o/publication.pptx"},
        {"kind": "file", "operation": operation, "fs_path": "w/o/publication.pdf"},
        {"kind": "file", "operation": operation, "fs_path": "w/o/notes.md"},
    ]
    assert _p(tool, {}, {"status": "failed", "error": "boom"}) == []


def test_verify_creative_studio_projects_verified_files_previews_and_receipt():
    payload = {
        "status": "completed", "technical_checks_passed": True,
        "final_files": [{"document_id": "d1", "fs_path": "w/o/publication.pptx", "name": "publication.pptx",
                         "file_size": 5, "kind": "document", "status": "completed", "qa": {}, "sha256": "h1"}],
        "previews": [{"document_id": "p1", "fs_path": "w/r/page-01.png", "name": "page-01.png",
                      "file_size": 3, "sha256": "h2", "kind": "image"}],
        "receipt": {"document_id": "r1", "fs_path": "w/r/verification.json", "name": "verification.json",
                    "file_size": 4, "sha256": "h3", "kind": "document"},
    }
    assert _p("verify_creative_studio", {}, payload) == [
        {"kind": "file", "operation": "verified", "document_id": "d1", "fs_path": "w/o/publication.pptx",
         "name": "publication.pptx", "sha256": "h1"},
        {"kind": "file", "operation": "preview", "document_id": "p1", "fs_path": "w/r/page-01.png",
         "name": "page-01.png", "sha256": "h2"},
        {"kind": "file", "operation": "verification_receipt", "document_id": "r1",
         "fs_path": "w/r/verification.json", "name": "verification.json", "sha256": "h3"},
    ]
    assert _p("verify_creative_studio", {}, {"status": "failed", "error": "Output changed"}) == []


def test_patch_file_projects_the_patched_file():
    payload = {"patched": True, "operations_applied": 1, "path": "docs/a.md", "size": 10,
               "source_sha256": "h1", "mtime_ns": 1, "knowledge_synced": True, "document_id": "d1"}
    assert _p("patch_file", {"path": "docs/a.md"}, payload) == [
        {"kind": "file", "operation": "patch", "document_id": "d1", "fs_path": "docs/a.md", "sha256": "h1"},
    ]
    rolled_back = {"error": "Knowledge projection failed ...", "patched": False, "knowledge_synced": False}
    assert _p("patch_file", {"path": "docs/a.md"}, rolled_back) == []


def test_replace_file_projects_the_replaced_target():
    payload = {"replaced": True, "unchanged": False, "source_path": "new.docx", "target_path": "old.docx",
               "size": 5, "source_sha256": "src", "target_sha256": "src", "document_id": "d2"}
    assert _p("replace_file", {}, payload) == [
        {"kind": "file", "operation": "replace", "document_id": "d2", "fs_path": "old.docx", "sha256": "src"},
    ]
    unchanged = {"replaced": False, "unchanged": True, "source_path": "a", "target_path": "b",
                 "source_sha256": "x", "target_sha256": "x"}
    assert _p("replace_file", {}, unchanged) == []


def test_archive_file_collection_projects_only_archived_copies():
    payload = {"files": [
        {"role": "video", "document_id": "a1", "name": "video.mp4", "source_sha256": "h1",
         "viewer_url": "/v/a1", "source_document_id": "s1", "archive_path": "w/archive/video.mp4"},
        {"role": "notes", "document_id": "s2", "name": "notes.md"},  # not archived: passed through
    ], "archive_folder": "w/archive"}
    assert _p("archive_file_collection", {}, payload) == [
        {"kind": "file", "operation": "archive", "document_id": "a1", "fs_path": "w/archive/video.mp4",
         "url": "/v/a1", "name": "video.mp4", "sha256": "h1"},
    ]


def test_code_project_write_projects_written_files_at_entity_paths():
    payload = {"project_id": "p1", "root_path": "Projects/site", "written": [
        {"path": "index.html", "size": 3, "sha256": "h1", "created": True, "document_id": "d1"},
        {"path": "css/app.css", "size": 4, "sha256": "h2", "created": False, "document_id": None},
    ], "deleted": [], "validation": {}}
    assert _p("code_project", {"action": "write"}, payload) == [
        {"kind": "file", "operation": "code_write", "document_id": "d1", "fs_path": "Projects/site/index.html",
         "sha256": "h1"},
        {"kind": "file", "operation": "code_write", "fs_path": "Projects/site/css/app.css", "sha256": "h2"},
    ]
    deletes_only = {"project_id": "p1", "root_path": "Projects/site", "written": [], "deleted": ["old.html"]}
    assert _p("code_project", {"action": "write"}, deletes_only) == []


def test_code_project_create_projects_the_new_project():
    payload = {"created": True, "project_id": "p1", "name": "Site", "profile": "static_site",
               "root_path": "Projects/site", "entry": "index.html"}
    assert _p("code_project", {"action": "create"}, payload) == [
        {"kind": "code_project", "operation": "create", "fs_path": "Projects/site", "name": "Site",
         "external_id": "p1"},
    ]
    assert _p("code_project", {"action": "create"}, {**payload, "created": False}) == []
    assert _p("code_project", {"action": "get"}, payload) == []


def test_sandbox_save_result_projects_the_saved_file():
    payload = {"saved": True, "saved_to_knowledge": True, "document_id": "d1", "name": "report.pdf",
               "file_size": 9, "fs_path": "w/report.pdf", "result_url": "/api/v1/fs/e/w/report.pdf"}
    assert _p("sandbox_save_result", {}, payload) == [
        {"kind": "file", "operation": "save_result", "document_id": "d1", "fs_path": "w/report.pdf",
         "url": "/api/v1/fs/e/w/report.pdf", "name": "report.pdf"},
    ]
    assert _p("sandbox_save_result", {}, {"error": "sandbox_download_failed"}) == []


def test_sandbox_composite_save_result_reaches_the_ledger_as_the_inner_tool():
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("sandbox", "s-1", json.dumps({
        "saved": True, "saved_to_knowledge": False, "fs_path": "w/out.csv", "name": "out.csv",
    }), {"action": "save_result", "params": {"sandbox_id": "sb", "file_path": "/out.csv", "filename": "out.csv"}})

    assert [(r["tool"], r["fs_path"]) for r in ledger.checkpoint_state()["receipts"]] == [
        ("sandbox_save_result", "w/out.csv"),
    ]
