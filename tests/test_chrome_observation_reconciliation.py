"""Reconcile real action observations without model-selected action IDs."""

import json

import pytest

from packages.core.ai.runtime.chrome_action_outcomes import record_action_outcome
from packages.core.services.assistant_blocks import AssistantBlocksBuilder


def record(state, tool, payload, args=None):
    return json.loads(
        record_action_outcome(
            tool_name=f"mcp__chrome__{tool}", arguments=args or {}, result=json.dumps(payload), state=state
        )
    )


def scope(media=None, **kwargs):
    return {"scope_id": "editor-instance-1", "complete": True, "media": media or [], **kwargs}


def page(snapshot="before", observation=None, **kwargs):
    return {
        "ok": True,
        "tabId": 7,
        "url": "https://example.test/compose",
        "snapshot_id": snapshot,
        "pageContent": "Compose",
        "page_feedback": [],
        "upload_candidates": [{"selector": "#upload", "frame_id": 0, "upload_observation": observation or scope()}],
        **kwargs,
    }


def uploaded(state):
    record(state, "read_page", page())
    return record(
        state,
        "upload",
        {
            "ok": True,
            "tabId": 7,
            "action_executed": True,
            "verification": {"status": "pending"},
            "files_count": 1,
            "submitted_files": [{"name": "photo.png"}],
            "upload_observation": scope(frame_id=0),
        },
    )


def preview(**kwargs):
    return {"source": "blob:https://example.test/new-photo", "kind": "image", "ready": True, **kwargs}


def test_upload_is_confirmed_by_fresh_scoped_preview_without_filename_or_verification_arguments():
    state = {}
    action = uploaded(state)
    observed = record(state, "read_page", page("after", scope([preview()])))
    proof = observed.get("action_verification", {})
    assert proof.get("status") == "verified"
    assert proof["action_id"] == action["action_receipt"]["action_id"]
    assert proof["condition"] == "attachment_preview"
    assert "website_upload_confirmed" not in observed
    builder = AssistantBlocksBuilder()
    builder.end_tool("mcp__chrome__upload", structured_result=action, status="unverified")
    builder.end_tool("mcp__chrome__read_page", structured_result=observed, status="success")
    builder.set_final_text("Draft ready")
    assert builder.blocks()[0]["status"] == "completed"
    assert builder.blocks()[0]["steps"][0]["verified_by_step_id"]


@pytest.mark.parametrize(
    "case",
    [
        "foreign_tab",
        "foreign_scope",
        "incomplete",
        "old_snapshot",
        "unloaded",
        "remote_image",
        "wrong_kind",
        "extra_media",
        "intervening_action",
        "error_feedback",
    ],
)
def test_automatic_preview_reconciliation_never_uses_unattributable_evidence(case):
    state = {}
    action = uploaded(state)
    p = page("after", scope([preview()]))
    area = p["upload_candidates"][0]["upload_observation"]
    if case == "foreign_tab":
        p["tabId"] = 8
    elif case == "foreign_scope":
        area["scope_id"] = "other-editor"
    elif case == "incomplete":
        area["complete"] = False
    elif case == "old_snapshot":
        p["snapshot_id"] = "before"
    elif case == "unloaded":
        area["media"][0]["ready"] = False
    elif case == "remote_image":
        area["media"][0]["source"] = "https://example.test/avatar.png"
    elif case == "wrong_kind":
        area["media"][0]["kind"] = "video"
    elif case == "extra_media":
        area["media"].append(preview(source="blob:second"))
    elif case == "intervening_action":
        record(state, "click_element", {"ok": True, "tabId": 7})
    elif case == "error_feedback":
        p["page_feedback"] = [{"severity": "error", "text": "Upload rejected"}]
    observed = record(state, "read_page", p)
    assert observed.get("action_verification", {}).get("status") != "verified"
    assert state["action_receipts"][action["action_receipt"]["action_id"]]["receipt"]["verification"] == "pending"


def test_preexisting_preview_is_not_new_attachment_evidence():
    # Use an actual baseline with an existing preview, not a later unrelated read.
    state = {}
    record(state, "read_page", page(observation=scope([preview()])))
    record(
        state,
        "upload",
        {
            "ok": True,
            "tabId": 7,
            "action_executed": True,
            "verification": {"status": "pending"},
            "submitted_files": [{"name": "photo.png"}],
            "files_count": 1,
            "upload_observation": scope([preview()], frame_id=0),
        },
    )
    result = record(state, "read_page", page("after", scope([preview()])))
    assert result.get("action_verification", {}).get("status") != "verified"


def test_plain_read_returns_action_hint_when_evidence_is_not_ready():
    state = {}
    action = uploaded(state)
    result = record(state, "read_page", page("after"))
    assert result["pending_verification"]["action_id"] == action["action_receipt"]["action_id"]
    assert result["pending_verification"]["condition"] == "attachment_preview"


def test_exact_field_value_uses_stable_target_not_reused_ref():
    state = {}
    field = {"ref": "e1", "selector": "#caption", "frame_id": 0, "role": "textbox", "value": ""}
    record(state, "read_page", page(semantic_refs=[field]))
    action = record(
        state,
        "fill_or_select",
        {"ok": True, "tabId": 7, "target_resolution": {"selector": "#caption", "frame_id": 0}},
        {"ref": "e1", "value": "Draft"},
    )
    wrong = record(state, "read_page", page("wrong", semantic_refs=[{**field, "selector": "#other", "value": "Draft"}]))
    assert wrong.get("action_verification", {}).get("status") != "verified"
    right = record(state, "read_page", page("right", semantic_refs=[{**field, "ref": "e8", "value": "Draft"}]))
    assert right["action_verification"]["status"] == "verified"
    assert right["action_verification"]["action_id"] == action["action_receipt"]["action_id"]


def test_explicit_condition_can_omit_action_id_and_bind_current_pending_action():
    state = {}
    action = uploaded(state)
    result = record(
        state,
        "read_page",
        page("after", pageContent="Photo rejected", page_feedback=[{
            "text": "Photo rejected", "severity": "error", "scope_id": "editor-instance-1", "frame_id": 0,
        }]),
        {"verification": {"text": "Photo rejected", "outcome": "failed"}},
    )
    assert result["action_verification"]["status"] == "failed"
    assert result["action_verification"]["action_id"] == action["action_receipt"]["action_id"]


@pytest.mark.parametrize("text", ["", "Compose " * 8000], ids=["textless", "truncated"])
def test_typed_preview_does_not_require_complete_page_text(text):
    state = {}
    record(state, "read_page", page(pageContent=text))
    record(state, "upload", {
        "ok": True, "tabId": 7, "action_executed": True,
        "verification": {"status": "pending"}, "submitted_files": [{"name": "photo.png"}],
        "upload_observation": scope(frame_id=0),
    })
    result = record(state, "read_page", page("after", scope([preview()]), pageContent=""))
    assert result["action_verification"]["status"] == "verified"


@pytest.mark.parametrize("feedback_scope", [None, "other-editor", "editor-instance-1"])
def test_text_never_overrides_missing_typed_upload_evidence(feedback_scope):
    state = {}
    uploaded(state)
    result = record(state, "read_page", page("after", pageContent="Success", page_feedback=[{
        "text": "Success", "scope_id": feedback_scope, "severity": "success",
    }]), {"verification": {"text": "Success"}})
    assert result["action_verification"]["status"] == "unverified"


@pytest.mark.parametrize("case", ["unscoped", "other_scope", "other_frame", "preexisting", "scoped"])
def test_untyped_result_text_requires_new_feedback_from_same_action_scope(case):
    state = {}
    feedback = {"text": "Report ready", "scope_id": "editor-instance-1", "frame_id": 0}
    record(state, "read_page", page(page_feedback=[feedback] if case == "preexisting" else []))
    record(state, "upload", {
        "ok": True, "tabId": 7, "action_executed": True, "verification": {"status": "pending"},
        "submitted_files": [{"name": "report.pdf"}], "upload_observation": scope(frame_id=0),
    })
    if case == "unscoped":
        feedback.pop("scope_id")
    elif case == "other_scope":
        feedback["scope_id"] = "other-editor"
    elif case == "other_frame":
        feedback["frame_id"] = 2
    result = record(state, "read_page", page("after", page_feedback=[feedback]),
                    {"verification": {"text": "Report ready"}})
    assert (result["action_verification"]["status"] == "verified") is (case == "scoped")


def test_typed_field_can_verify_without_page_text():
    state = {}
    field = {"selector": "#caption", "frame_id": 0, "role": "textbox", "value": ""}
    record(state, "read_page", page(pageContent="", semantic_refs=[field]))
    record(state, "fill_or_select", {"ok": True, "tabId": 7, "target_resolution": field}, {"value": "Draft"})
    result = record(state, "read_page", page("after", pageContent="", semantic_refs=[{**field, "value": "Draft"}]))
    assert result["action_verification"]["status"] == "verified"


@pytest.mark.parametrize("case", ["saturated", "missing", "truncated"])
def test_incomplete_feedback_baseline_cannot_prove_new_explicit_text(case):
    state = {}
    baseline = page(page_feedback=[{"text": f"Notice {n}", "scope_id": "editor-instance-1"} for n in range(8)])
    if case == "missing":
        baseline.pop("page_feedback")
    elif case == "truncated":
        baseline["page_feedback"] = [{"text": "Long notice...", "scope_id": "editor-instance-1", "text_truncated": True}]
    record(state, "read_page", baseline)
    record(state, "upload", {
        "ok": True, "tabId": 7, "action_executed": True, "verification": {"status": "pending"},
        "submitted_files": [{"name": "report.pdf"}], "upload_observation": scope(frame_id=0),
    })
    result = record(state, "read_page", page("after", page_feedback=[{
        "text": "Report ready", "scope_id": "editor-instance-1", "frame_id": 0,
    }]), {"verification": {"text": "Report ready"}})
    assert result["action_verification"]["status"] == "unverified"


def test_field_value_from_replacement_editor_with_same_selector_is_not_proof():
    state = {}
    field = {"selector": "#caption", "frame_id": 0, "role": "textbox", "value": "", "scope_id": "editor-instance-1"}
    record(state, "read_page", page(semantic_refs=[field]))
    record(state, "fill_or_select", {"ok": True, "tabId": 7, "target_resolution": field}, {"value": "Draft"})
    result = record(state, "read_page", page("after", semantic_refs=[{**field, "value": "Draft", "scope_id": "editor-2"}]))
    assert result["action_verification"]["status"] == "unverified"


def test_upload_public_label_uses_submitted_name_not_selector():
    builder = AssistantBlocksBuilder()
    builder.end_tool(
        "mcp__chrome__upload",
        arguments={"selector": "div > div > input", "document_id": "opaque"},
        structured_result={"submitted_files": [{"name": "photo.png"}]},
    )
    step = builder.blocks()[0]["steps"][0]
    assert step["display_key"].endswith("browser.upload")
    assert step["display_params"] == {"target": "photo.png"}


def test_fill_public_label_does_not_expose_ref():
    builder = AssistantBlocksBuilder()
    builder.end_tool(
        "mcp__chrome__fill_or_select",
        arguments={"ref": "e1", "value": "draft"},
        structured_result={"target_resolution": {"label": "Caption"}},
    )
    step = builder.blocks()[0]["steps"][0]
    assert step["display_key"].endswith("browser.fill")
    assert step["display_params"] == {"target": "Caption"}


def test_fill_uses_its_verified_post_action_value_without_an_extra_read():
    state = {}
    field = {"selector": "#caption", "role": "textbox", "frame_id": 0, "value": ""}
    record(state, "read_page", page(semantic_refs=[field]))
    post = page("after", semantic_refs=[{**field, "value": "Draft"}], state_verified=True)
    result = record(
        state,
        "fill_or_select",
        {"ok": True, "tabId": 7, "target_resolution": field, "post_action_page_state": post},
        {"value": "Draft"},
    )
    assert result["action_receipt"]["verification"] == "verified"
    redundant = record(
        state,
        "read_page",
        {**post, "snapshot_id": "later"},
        {"verification": {"action_id": result["action_receipt"]["action_id"], "text": "Draft"}},
    )
    assert redundant["action_verification"]["status"] == "already_verified"


def test_pending_action_hint_survives_context_compaction():
    from packages.core.ai.agentic_loop import _compact_tool_result_for_context

    state = {}
    action = uploaded(state)
    observation = record(state, "read_page", page("after", pageContent="Compose " * 2000))
    result = json.loads(
        _compact_tool_result_for_context("mcp__chrome__read_page", json.dumps(observation), max_chars=1800)
    )
    assert result["pending_verification"]["action_id"] == action["action_receipt"]["action_id"]


def test_history_relabels_chrome_details_without_changing_the_original_result():
    from packages.core.services.assistant_blocks import project_browser_display_names

    blocks = [
        {
            "type": "process",
            "status": "unverified",
            "steps": [
                {
                    "name": "mcp__chrome__upload",
                    "seq": 1,
                    "status": "unverified",
                    "display_key": "old",
                    "display_params": {"target": "div > input"},
                }
            ],
        }
    ]
    calls = [
        {
            "name": "mcp__chrome__upload",
            "arguments": {"selector": "div > input"},
            "raw_result": json.dumps({"submitted_files": [{"name": "photo.png"}]}),
        }
    ]
    projected = project_browser_display_names(blocks, calls)
    assert projected[0]["steps"][0]["display_params"] == {"target": "photo.png"}
    assert projected[0]["steps"][0]["status"] == "unverified"
    assert blocks[0]["steps"][0]["display_key"] == "old"
