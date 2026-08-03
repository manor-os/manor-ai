"""Runtime routing helpers for the user's local Chrome browser."""
from __future__ import annotations

import json
import logging
import re
from hashlib import sha256
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


GENERIC_WEB_TOOLS = frozenset({"web_search", "web_fetch", "browse_web"})

CHROME_MCP_TOOLS = frozenset({
    "mcp__chrome__documentation",
    "mcp__chrome__capabilities",
    "mcp__chrome__status",
    "mcp__chrome__reload_extension",
    "mcp__chrome__open",
    "mcp__chrome__open_new_tab",
    "mcp__chrome__open_or_reuse",
    "mcp__chrome__name_session",
    "mcp__chrome__confirm_action",
    "mcp__chrome__navigate",
    "mcp__chrome__open_tabs",
    "mcp__chrome__selected_tab",
    "mcp__chrome__tab_info",
    "mcp__chrome__back",
    "mcp__chrome__forward",
    "mcp__chrome__reload",
    "mcp__chrome__js_dialog",
    "mcp__chrome__handle_js_dialog",
    "mcp__chrome__list_tabs",
    "mcp__chrome__get_windows_and_tabs",
    "mcp__chrome__get_group_state",
    "mcp__chrome__start_tab_recording",
    "mcp__chrome__switch_tab_recording_source",
    "mcp__chrome__get_tab_recording",
    "mcp__chrome__stop_tab_recording",
    "mcp__chrome__cancel_tab_recording",
    "mcp__chrome__finalize_tabs",
    "mcp__chrome__close_group_tabs",
    "mcp__chrome__claim_tab",
    "mcp__chrome__activate_tab",
    "mcp__chrome__close_tab",
    "mcp__chrome__switch_tab",
    "mcp__chrome__close_tabs",
    "mcp__chrome__ping_tab",
    "mcp__chrome__read_page",
    "mcp__chrome__computer",
    "mcp__chrome__wait",
    "mcp__chrome__get_interactive_elements",
    "mcp__chrome__inspect_selector",
    "mcp__chrome__get_web_content",
    "mcp__chrome__click_element",
    "mcp__chrome__hover",
    "mcp__chrome__fill_or_select",
    "mcp__chrome__scroll",
    "mcp__chrome__scroll_wheel",
    "mcp__chrome__click_point",
    "mcp__chrome__type_text",
    "mcp__chrome__press_key",
    "mcp__chrome__keyboard",
    "mcp__chrome__upload",
    "mcp__chrome__download",
    "mcp__chrome__wait_download",
    "mcp__chrome__screenshot",
    "mcp__chrome__clipboard_read",
    "mcp__chrome__clipboard_write",
    "mcp__chrome__viewport",
    "mcp__chrome__page_assets",
    "mcp__chrome__history",
    "mcp__chrome__console_logs",
    "mcp__chrome__send_cdp",
    "mcp__chrome__inject_script",
    "mcp__chrome__set_cursor",
    "mcp__chrome__hide_cursor",
    "mcp__chrome__set_badge",
})

CHROME_KNOWLEDGE_LOCAL_MCP_TOOLS = frozenset({
    "mcp__chrome_knowledge_local__prepare_upload",
})

CHROME_LOCAL_BROWSER_DEFAULT_TOOLS = (
    "mcp__chrome__open_or_reuse",
    "mcp__chrome__read_page",
    "mcp__chrome__click_element",
    "mcp__chrome__fill_or_select",
    "mcp__chrome__wait",
    "mcp__chrome__finalize_tabs",
    "mcp__chrome__computer",
    "mcp__chrome__hover",
    "mcp__chrome__press_key",
    "mcp__chrome__scroll",
    "mcp__chrome__upload",
    "mcp__chrome__download",
    "mcp__chrome__wait_download",
    "mcp__chrome__screenshot",
    "mcp__chrome__confirm_action",
)

_URL_RE = re.compile(r"https?://[^\s`\"'）)]+", re.IGNORECASE)
_CHROME_TERMS = (
    "chrome",
    "google chrome",
    "manor chrome",
    "local chrome",
    "chrome浏览器",
    "谷歌浏览器",
    "谷歌 chrome",
)
_LOCAL_BROWSER_TERMS = (
    "local browser",
    "本地浏览器",
)
_CHROME_ACTION_TERMS = (
    "open",
    "navigate",
    "go to",
    "visit",
    "load",
    "read_page",
    "inspect",
    "read",
    "publish",
    "post",
    "share",
    "click",
    "scroll",
    "type",
    "fill",
    "tab",
    "tabs",
    "screenshot",
    "mouse",
    "打开",
    "访问",
    "进入",
    "观察",
    "识别",
    "读取",
    "点击",
    "滚动",
    "输入",
    "填写",
    "标签",
    "截图",
    "鼠标",
    "移动",
)
_CHROME_NODE_ACTION_TOOLS = frozenset({
    "mcp__chrome__click_element",
    "mcp__chrome__hover",
    "mcp__chrome__fill_or_select",
})
_CHROME_RUNTIME_CONTRACT_STATE_KEY = "chrome_runtime_contract_v1"
_CHROME_WORKFLOW_SETUP_TOOLS = frozenset({
    "mcp__chrome__documentation",
    "mcp__chrome__capabilities",
    "mcp__chrome__status",
    "mcp__chrome__reload_extension",
})
_CHROME_WORKFLOW_DOCUMENTATION_EXEMPT_TOOLS = _CHROME_WORKFLOW_SETUP_TOOLS | frozenset({
    "mcp__chrome__confirm_action",
    "mcp__chrome__close_group_tabs",
    "mcp__chrome__finalize_tabs",
})
_CHROME_WORKFLOW_AFTER_FINALIZE_ALLOWED_TOOLS = _CHROME_WORKFLOW_SETUP_TOOLS | frozenset({
    "mcp__chrome__close_group_tabs",
    "mcp__chrome__get_group_state",
})
_CHROME_RECORDING_TOOLS = frozenset({
    "mcp__chrome__start_tab_recording",
    "mcp__chrome__switch_tab_recording_source",
    "mcp__chrome__get_tab_recording",
    "mcp__chrome__stop_tab_recording",
    "mcp__chrome__cancel_tab_recording",
})
_CHROME_READ_PAGE_REQUIRED_REASONS = frozenset({
    "snapshot_id_required",
    "snapshot_mismatch",
    "fallback_reason_required",
})
_CHROME_PAGE_MUTATION_TOOLS = frozenset({
    "mcp__chrome__open",
    "mcp__chrome__open_new_tab",
    "mcp__chrome__open_or_reuse",
    "mcp__chrome__navigate",
    "mcp__chrome__back",
    "mcp__chrome__forward",
    "mcp__chrome__reload",
    "mcp__chrome__handle_js_dialog",
    "mcp__chrome__click_element",
    "mcp__chrome__fill_or_select",
    "mcp__chrome__scroll",
    "mcp__chrome__scroll_wheel",
    "mcp__chrome__click_point",
    "mcp__chrome__type_text",
    "mcp__chrome__press_key",
    "mcp__chrome__keyboard",
    "mcp__chrome__upload",
    "mcp__chrome__inject_script",
})
_CHROME_TRUSTED_POST_ACTION_TOOLS = frozenset({
    "mcp__chrome__click_element",
    "mcp__chrome__fill_or_select",
    "mcp__chrome__type_text",
    "mcp__chrome__press_key",
})
_CHROME_NO_PROGRESS_PASSIVE_TOOLS = frozenset({
    "mcp__chrome__read_page",
    "mcp__chrome__screenshot",
    "mcp__chrome__wait",
})
_CHROME_NAVIGATION_RESET_TOOLS = frozenset({
    "mcp__chrome__open",
    "mcp__chrome__open_new_tab",
    "mcp__chrome__open_or_reuse",
    "mcp__chrome__navigate",
    "mcp__chrome__back",
    "mcp__chrome__forward",
    "mcp__chrome__reload",
})
_CHROME_OBSERVATION_AFTER_ACTION_ALLOWED_TOOLS = _CHROME_WORKFLOW_SETUP_TOOLS | frozenset({
    "mcp__chrome__read_page",
    "mcp__chrome__wait",
    "mcp__chrome__screenshot",
    "mcp__chrome__computer",
    "mcp__chrome__tab_info",
    "mcp__chrome__js_dialog",
    "mcp__chrome__get_group_state",
    "mcp__chrome__list_tabs",
    "mcp__chrome__get_windows_and_tabs",
    "mcp__chrome__selected_tab",
    "mcp__chrome__console_logs",
    "mcp__chrome__page_assets",
    "mcp__chrome__viewport",
    "mcp__chrome__send_cdp",
    "mcp__chrome__finalize_tabs",
    "mcp__chrome__close_group_tabs",
})
_CHROME_WORKFLOW_REQUIRES_SESSION_NAME_TOOLS = frozenset({
    "mcp__chrome__open",
    "mcp__chrome__open_new_tab",
    "mcp__chrome__open_or_reuse",
    "mcp__chrome__navigate",
    "mcp__chrome__claim_tab",
    "mcp__chrome__activate_tab",
    "mcp__chrome__switch_tab",
    "mcp__chrome__close_tab",
    "mcp__chrome__close_tabs",
    "mcp__chrome__ping_tab",
    "mcp__chrome__tab_info",
    "mcp__chrome__back",
    "mcp__chrome__forward",
    "mcp__chrome__reload",
    "mcp__chrome__js_dialog",
    "mcp__chrome__handle_js_dialog",
    "mcp__chrome__read_page",
    "mcp__chrome__computer",
    "mcp__chrome__wait",
    "mcp__chrome__get_interactive_elements",
    "mcp__chrome__inspect_selector",
    "mcp__chrome__get_web_content",
    "mcp__chrome__click_element",
    "mcp__chrome__hover",
    "mcp__chrome__fill_or_select",
    "mcp__chrome__scroll",
    "mcp__chrome__scroll_wheel",
    "mcp__chrome__click_point",
    "mcp__chrome__type_text",
    "mcp__chrome__press_key",
    "mcp__chrome__keyboard",
    "mcp__chrome__upload",
    "mcp__chrome__download",
    "mcp__chrome__wait_download",
    "mcp__chrome__screenshot",
    "mcp__chrome__clipboard_read",
    "mcp__chrome__clipboard_write",
    "mcp__chrome__viewport",
    "mcp__chrome__page_assets",
    "mcp__chrome__console_logs",
    "mcp__chrome__send_cdp",
    "mcp__chrome__inject_script",
    "mcp__chrome__set_cursor",
    "mcp__chrome__hide_cursor",
    "mcp__chrome__set_badge",
})
_CHROME_SUPPORTED_KEY_NAMES = frozenset({
    "Backspace",
    "Delete",
    "End",
    "Enter",
    "Escape",
    "Home",
    "PageDown",
    "PageUp",
    "Return",
    "Space",
    "Tab",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "ArrowUp",
})
_CHROME_REF_RE = re.compile(r"^e\d+$")


@dataclass(frozen=True)
class ChromeLocalBrowserRoute:
    """Explicit request to operate the user's paired local Chrome."""

    @property
    def provider_keys(self) -> tuple[str, ...]:
        return ("chrome", "chrome_knowledge_local")

    @property
    def allowed_tool_names(self) -> set[str]:
        return set(CHROME_MCP_TOOLS | CHROME_KNOWLEDGE_LOCAL_MCP_TOOLS)

    @property
    def preferred_tool_names(self) -> tuple[str, ...]:
        return CHROME_LOCAL_BROWSER_DEFAULT_TOOLS


def detect_chrome_local_browser_route(text: str | None) -> ChromeLocalBrowserRoute | None:
    """Detect explicit local Chrome operation requests for Runtime Harness scope."""
    if not text:
        return None
    lowered = text.lower()
    names_chrome = any(term in lowered for term in _CHROME_TERMS)
    names_local_browser = any(term in lowered for term in _LOCAL_BROWSER_TERMS)
    if not names_chrome and not names_local_browser:
        return None
    has_url = bool(_URL_RE.search(text))
    has_browser_action = any(term in lowered for term in _CHROME_ACTION_TERMS)
    if not has_url and not has_browser_action:
        return None
    return ChromeLocalBrowserRoute()


def chrome_local_browser_intent(text: str | None) -> bool:
    return detect_chrome_local_browser_route(text) is not None


def runtime_blocked_generic_web_for_chrome_local_browser(
    *,
    tool_name: str,
    active_user_message: str | None,
) -> str | None:
    """Return a runtime block payload when explicit Chrome work uses web tools."""
    if tool_name not in GENERIC_WEB_TOOLS:
        return None
    if not detect_chrome_local_browser_route(active_user_message):
        return None
    chrome_worker_label = "local worker"
    return json.dumps({
        "status": "blocked",
        "reason": "chrome_local_browser_required",
        "blocked_tool": tool_name,
        "message": (
            "This request explicitly asks to operate the user's local Chrome "
            f"browser. Use the Chrome runtime skill backed by the paired {chrome_worker_label} "
            "instead of web_search, web_fetch, browse_web, "
            "or direct parent-chat Chrome MCP discovery."
        ),
        "next_step": (
            "Return to the normal Chrome skill path. If the `chrome` skill is "
            "listed in Available Skills, call invoke_skill with skill=\"chrome\" "
            "and the latest user request. If invoke_skill is deferred, load "
            "invoke_skill with search_tools; do not load Chrome MCP tools "
            "directly from the parent chat. The Chrome skill owns the "
            "mcp-chrome-style mcp__chrome__open_or_reuse -> "
            "mcp__chrome__read_page -> ref action -> "
            "mcp__chrome__read_page loop through the Runtime Harness."
        ),
    }, ensure_ascii=False)


def runtime_blocked_chrome_open_shortcut(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    active_user_message: str | None,
) -> str | None:
    """Block legacy open for ordinary Chrome tasks; clear aliases are allowed."""
    if tool_name != "mcp__chrome__open":
        return None
    if not detect_chrome_local_browser_route(active_user_message):
        return None
    if _chrome_explicit_new_tab_requested(arguments or {}):
        return None
    return json.dumps({
        "status": "blocked",
        "reason": "chrome_open_requires_explicit_new_tab",
        "blocked_tool": tool_name,
        "message": (
            "mcp__chrome__open is a compatibility-only legacy entry point "
            "that always creates a new Chrome tab. For normal Chrome tasks, "
            "reuse the task Browser Group tab with mcp__chrome__open_or_reuse. "
            "When a separate tab is genuinely required, call "
            "mcp__chrome__open_new_tab instead."
        ),
        "next_required_tool": "mcp__chrome__open_or_reuse",
        "replacement_tool": "mcp__chrome__open_or_reuse",
        "explicit_new_tab_tool": "mcp__chrome__open_new_tab",
    }, ensure_ascii=False)


def _chrome_explicit_new_tab_requested(arguments: dict[str, Any]) -> bool:
    for key in (
        "explicitNewTab",
        "explicit_new_tab",
        "newTab",
        "new_tab",
        "forceNewTab",
        "force_new_tab",
        "openNewTab",
        "open_new_tab",
    ):
        if arguments.get(key) is True:
            return True
    return False


def runtime_blocked_chrome_workflow_contract(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    runtime_metadata: dict[str, Any] | None,
    active_user_message: str | None = None,
) -> str | None:
    """Enforce the Codex-style Chrome workflow order inside a Runtime run.

    This is intentionally a Runtime/Manor AI guard, not a raw MCP gateway
    constraint. Direct diagnostics and smoke tests can still call the low-level
    MCP package, while ordinary Chrome tasks are steered through
    task-scoped tab work -> finalize.
    """
    canonical_tool = _canonical_chrome_tool_name(tool_name)
    if canonical_tool not in CHROME_MCP_TOOLS:
        return None
    if active_user_message is not None and not detect_chrome_local_browser_route(active_user_message):
        return None
    if runtime_metadata is None:
        return None
    state = _chrome_runtime_contract_state(runtime_metadata)

    if canonical_tool == "mcp__chrome__finalize_tabs" and state.get("active_recording_id"):
        return json.dumps({
            "status": "blocked",
            "reason": "chrome_recording_active",
            "blocked_tool": canonical_tool,
            "recording_id": state["active_recording_id"],
            "message": "Stop or cancel the active Chrome recording before finalizing Browser Group tabs.",
            "next_required_tools": [
                "mcp__chrome__stop_tab_recording",
                "mcp__chrome__cancel_tab_recording",
            ],
        }, ensure_ascii=False)

    if (
        canonical_tool == "mcp__chrome__finalize_tabs"
        and _workflow_scene_capture_context(arguments)
    ):
        screenshot_ids = _capture_receipt_ids(state, "ready_screenshot_document_ids")
        recording_ids = _capture_receipt_ids(state, "ready_recording_document_ids")
        missing_receipts = []
        if not screenshot_ids:
            missing_receipts.append("png_screenshot")
        if not recording_ids:
            missing_receipts.append("webm_recording")
        if missing_receipts:
            return json.dumps({
                "status": "blocked",
                "reason": "workflow_scene_capture_receipts_required",
                "blocked_tool": canonical_tool,
                "missing_receipts": missing_receipts,
                "message": (
                    "This approved Workflow scene still needs its durable capture receipts. "
                    "Stop exploratory reads and clicks, capture the verified state, and return "
                    "one ready PNG screenshot receipt plus one completed WebM recording receipt."
                ),
                "next_required_tools": [
                    "mcp__chrome__screenshot",
                    "mcp__chrome__start_tab_recording",
                    "mcp__chrome__stop_tab_recording",
                    "mcp__chrome__get_tab_recording",
                ],
            }, ensure_ascii=False)
        return json.dumps({
            "status": "blocked",
            "reason": "workflow_scene_capture_preserve_tabs",
            "blocked_tool": canonical_tool,
            "receipts_ready": True,
            "message": (
                "The approved Workflow scene has both durable capture receipts. "
                "Return those receipts now without finalizing tabs; the parent multi-scene "
                "Workflow owns the persistent Chrome session."
            ),
        }, ensure_ascii=False)

    if bool(state.get("finalized")) and canonical_tool not in _CHROME_WORKFLOW_AFTER_FINALIZE_ALLOWED_TOOLS:
        return json.dumps({
            "status": "blocked",
            "reason": "chrome_tabs_already_finalized",
            "blocked_tool": canonical_tool,
            "message": (
                "mcp__chrome__finalize_tabs already finalized this Chrome Browser Group for the current "
                "Runtime run. Treat finalize_tabs as the final Chrome action of the turn."
            ),
            "next_step": "Start a new Chrome task/session before doing more browser work.",
        }, ensure_ascii=False)

    blocked_retry = _blocked_chrome_action_before_read_page(
        canonical_tool=canonical_tool,
        arguments=arguments,
        state=state,
    )
    if blocked_retry is not None:
        return blocked_retry

    blocked_observation = _blocked_chrome_action_before_observation(
        canonical_tool=canonical_tool,
        state=state,
    )
    if blocked_observation is not None:
        return blocked_observation

    blocked_no_progress = _blocked_chrome_no_progress_exploration(
        canonical_tool=canonical_tool,
        state=state,
    )
    if blocked_no_progress is not None:
        return blocked_no_progress

    return None


def runtime_record_chrome_workflow_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    runtime_metadata: dict[str, Any] | None,
) -> None:
    """Record successful Chrome workflow milestones in Runtime metadata."""
    canonical_tool = _canonical_chrome_tool_name(tool_name)
    if canonical_tool not in CHROME_MCP_TOOLS or runtime_metadata is None:
        return
    state = _chrome_runtime_contract_state(runtime_metadata)
    state["last_tool"] = canonical_tool
    state["chrome_call_count"] = int(state.get("chrome_call_count") or 0) + 1
    counter_key = {
        "mcp__chrome__read_page": "read_page_call_count",
        "mcp__chrome__screenshot": "screenshot_call_count",
        "mcp__chrome__wait": "wait_call_count",
    }.get(canonical_tool)
    if counter_key:
        state[counter_key] = int(state.get(counter_key) or 0) + 1

    if canonical_tool == "mcp__chrome__documentation":
        topic = str((arguments or {}).get("topic") or "runtime-contract").strip() or "runtime-contract"
        if topic == "runtime-contract":
            state["runtime_contract_documented"] = True
    elif canonical_tool == "mcp__chrome__name_session":
        name = str(
            (arguments or {}).get("name")
            or (arguments or {}).get("groupTitle")
            or (arguments or {}).get("group_title")
            or ""
        ).strip()
        state["session_named"] = True
        if name:
            state["session_name"] = name
    elif canonical_tool == "mcp__chrome__finalize_tabs":
        state["finalized"] = True
        logger.info(
            "Chrome workflow summary %s",
            json.dumps(runtime_chrome_workflow_summary(runtime_metadata), sort_keys=True),
        )


def runtime_record_chrome_tool_result(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    runtime_metadata: dict[str, Any] | None,
) -> None:
    """Record Chrome result-driven recovery requirements in Runtime metadata."""
    canonical_tool = _canonical_chrome_tool_name(tool_name)
    if canonical_tool not in CHROME_MCP_TOOLS or runtime_metadata is None:
        return
    state = _chrome_runtime_contract_state(runtime_metadata)
    payload = _parse_json_object(result)
    if payload is not None and _workflow_scene_capture_context(arguments):
        _record_workflow_scene_capture_receipt(
            state,
            canonical_tool=canonical_tool,
            payload=payload,
        )
    if canonical_tool in _CHROME_RECORDING_TOOLS and payload is not None:
        recording_id = str(payload.get("recording_id") or "").strip()
        status = str(payload.get("status") or "").strip()
        if canonical_tool == "mcp__chrome__start_tab_recording" and status in {"preparing", "recording"}:
            if recording_id:
                state["active_recording_id"] = recording_id
        elif canonical_tool in {
            "mcp__chrome__stop_tab_recording",
            "mcp__chrome__cancel_tab_recording",
        } and status in {"upload_pending", "uploading", "completed", "cancelled", "failed"}:
            state.pop("active_recording_id", None)
            state["last_recording_status"] = status
        elif canonical_tool == "mcp__chrome__get_tab_recording" and status in {"completed", "cancelled", "failed"}:
            state.pop("active_recording_id", None)
            state["last_recording_status"] = status
    if canonical_tool == "mcp__chrome__read_page":
        if payload is not None and payload.get("ok") is True:
            state.pop("pending_read_page_required_action", None)
            state.pop("pending_observation_after_action", None)
            state.pop("post_action_read_credit", None)
            snapshot_id = str(payload.get("snapshot_id") or payload.get("snapshotId") or "").strip()
            if snapshot_id:
                _record_chrome_snapshot(state, snapshot_id=snapshot_id, from_read_page=True)
            _record_chrome_candidate_summary(state, payload)
        return

    if payload is None:
        return
    if canonical_tool == "mcp__chrome__screenshot":
        _record_chrome_screenshot(state)
    elif canonical_tool == "mcp__chrome__wait":
        _record_chrome_wait(state)

    reason = str(payload.get("reason") or "").strip()
    read_page_required = payload.get("read_page_required") is True or payload.get("readPageRequired") is True
    if reason not in _CHROME_READ_PAGE_REQUIRED_REASONS and not read_page_required:
        if _chrome_result_has_verified_post_action_state(
            canonical_tool,
            payload,
            arguments,
        ):
            post_action_state = payload["post_action_page_state"]
            _record_chrome_snapshot(
                state,
                snapshot_id=str(post_action_state.get("snapshot_id") or "").strip(),
                from_read_page=False,
            )
            _record_chrome_candidate_summary(state, post_action_state)
            state.pop("pending_observation_after_action", None)
            state.pop("post_action_read_credit", None)
            return
        if canonical_tool in _CHROME_NAVIGATION_RESET_TOOLS and _chrome_tool_result_requires_observation(
            canonical_tool,
            payload,
        ):
            _reset_chrome_exploration_budget(state, latest_snapshot_id=None)
        if _chrome_tool_result_requires_observation(canonical_tool, payload):
            state["pending_observation_after_action"] = {
                "tool_name": canonical_tool,
                "reason": "action_may_have_changed_page",
                "next_required_tool": "mcp__chrome__read_page",
            }
            state["post_action_read_credit"] = True
        return
    state.pop("pending_observation_after_action", None)
    state["pending_read_page_required_action"] = {
        "tool_name": canonical_tool,
        "signature": _chrome_action_signature(canonical_tool, arguments),
        "reason": reason or "read_page_required",
        "next_required_tool": "mcp__chrome__read_page",
    }
    state["post_action_read_credit"] = True


def _record_chrome_candidate_summary(state: dict[str, Any], payload: dict[str, Any]) -> None:
    source = payload.get("candidate_summary")
    if not isinstance(source, dict):
        source = payload
    summary = _bounded_chrome_candidate_summary(source)
    state["latest_candidate_summary"] = summary


def _bounded_chrome_candidate_summary(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    compacted: dict[str, Any] = {}
    counts = source.get("counts")
    if isinstance(counts, dict):
        compacted["counts"] = {
            key: int(counts.get(key) or 0)
            for key in (
                "semantic_refs",
                "input_candidates",
                "dialog_candidates",
                "upload_candidates",
                "submit_candidates",
                "next_actions",
            )
        }
    frame_summary = source.get("frame_summary")
    if isinstance(frame_summary, dict):
        compacted["frame_summary"] = {
            key: int(frame_summary.get(key) or 0)
            for key in ("total", "observed", "skipped", "hidden", "unresolved")
        }
    skipped_frames = source.get("skipped_frames")
    if isinstance(skipped_frames, list):
        compacted["skipped_frames"] = []
        for raw_frame in skipped_frames[:8]:
            if not isinstance(raw_frame, dict):
                continue
            frame: dict[str, Any] = {}
            for key in ("frame_id", "parent_frame_id"):
                value = raw_frame.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    frame[key] = int(value)
            for key in ("url", "status", "reason", "host_selector"):
                value = raw_frame.get(key)
                if isinstance(value, str):
                    frame[key] = value[:240]
            if isinstance(raw_frame.get("host_visible"), bool):
                frame["host_visible"] = raw_frame["host_visible"]
            compacted["skipped_frames"].append(frame)
    semantic_ref_keys = (
        "ref",
        "label",
        "role",
        "tag",
        "type",
        "href",
        "name",
        "autocomplete",
        "placeholder",
        "input_mode",
        "min",
        "max",
        "step",
        "pattern",
        "min_length",
        "max_length",
        "description",
        "validation_message",
        "selector",
        "frame_selector",
        "frame_url",
        "frame_id",
        "coordinate_space",
        "shadow_host",
        "shadow_selector",
        "aria_label",
        "title",
        "data_testid",
        "container_label",
        "form_label",
        "dialog_label",
        "required",
        "disabled",
        "read_only",
        "valid",
        "checked",
        "selected",
        "expanded",
        "editable",
        "clickable",
        "in_viewport",
        "bounds",
    )
    contracts = {
        "semantic_refs": (40, semantic_ref_keys),
        "input_candidates": (8, ("ref", "node_id", "label", "role")),
        "dialog_candidates": (2, ("label", "field_refs", "submit_refs")),
        "upload_candidates": (4, ("ref", "node_id", "selector", "label")),
        "submit_candidates": (4, ("ref", "node_id", "label")),
        "next_actions": (6, ("tool", "action", "ref", "node_id", "selector", "label")),
    }
    for key, (limit, keys) in contracts.items():
        raw_candidates = source.get(key)
        if not isinstance(raw_candidates, list):
            continue
        candidates: list[dict[str, Any]] = []
        for raw_candidate in raw_candidates[:limit]:
            if not isinstance(raw_candidate, dict):
                continue
            candidate: dict[str, Any] = {}
            for candidate_key in keys:
                raw_value = raw_candidate.get(candidate_key)
                if candidate_key in {"field_refs", "submit_refs"}:
                    candidate[candidate_key] = (
                        [str(item)[:160] for item in raw_value[:8] if isinstance(item, str) and item.strip()]
                        if isinstance(raw_value, list)
                        else []
                    )
                elif isinstance(raw_value, str):
                    candidate[candidate_key] = raw_value[:160]
                elif isinstance(raw_value, bool):
                    candidate[candidate_key] = raw_value
                elif isinstance(raw_value, (int, float)):
                    candidate[candidate_key] = raw_value
                elif candidate_key == "bounds" and isinstance(raw_value, dict):
                    candidate[candidate_key] = {
                        coordinate: raw_value[coordinate]
                        for coordinate in ("x", "y", "width", "height")
                        if isinstance(raw_value.get(coordinate), (int, float))
                    }
            candidates.append(candidate)
        compacted[key] = candidates
    return compacted


def _blocked_chrome_no_progress_exploration(
    *,
    canonical_tool: str,
    state: dict[str, Any],
) -> str | None:
    if canonical_tool not in _CHROME_NO_PROGRESS_PASSIVE_TOOLS:
        return None
    snapshot_id = str(state.get("latest_snapshot_id") or "").strip()
    if not snapshot_id:
        return None
    if canonical_tool == "mcp__chrome__read_page" and state.get("post_action_read_credit") is True:
        return None

    screenshots = {
        str(value).strip()
        for value in state.get("screenshotted_snapshot_ids", [])
        if str(value or "").strip()
    }
    read_count = int(state.get("same_snapshot_read_count") or 0)
    wait_count = int(state.get("waits_since_state_change") or 0)
    recovery_count = int(state.get("no_progress_recovery_count") or 0)
    exhausted = False
    if canonical_tool == "mcp__chrome__read_page":
        exhausted = exhausted or read_count >= 2
    elif canonical_tool == "mcp__chrome__screenshot":
        exhausted = exhausted or snapshot_id in screenshots
    elif canonical_tool == "mcp__chrome__wait":
        exhausted = exhausted or wait_count >= 1
    if not exhausted:
        return None

    state["no_progress_block_count"] = int(state.get("no_progress_block_count") or 0) + 1
    candidate_summary = state.get("latest_candidate_summary")
    available_recovery_tools = [
        "mcp__chrome__click_element",
        "mcp__chrome__fill_or_select",
        "mcp__chrome__upload",
        "mcp__chrome__open_or_reuse",
        "mcp__chrome__navigate",
        "mcp__chrome__confirm_action",
        "mcp__chrome__finalize_tabs",
    ]
    if read_count < 2:
        available_recovery_tools.insert(0, "mcp__chrome__read_page")
    if snapshot_id not in screenshots:
        available_recovery_tools.insert(0, "mcp__chrome__screenshot")
    if wait_count < 1:
        available_recovery_tools.insert(0, "mcp__chrome__wait")
    return json.dumps({
        "status": "blocked",
        "reason": "chrome_no_progress_recovery_required",
        "blocked_tool": canonical_tool,
        "current_snapshot_id": snapshot_id,
        "same_snapshot_read_count": read_count,
        "screenshotted_current_snapshot": snapshot_id in screenshots,
        "waits_since_state_change": wait_count,
        "no_progress_recovery_count": recovery_count,
        **({"candidate_summary": candidate_summary} if isinstance(candidate_summary, dict) else {}),
        "available_recovery_tools": available_recovery_tools,
        "message": (
            "This passive Chrome observation was already used for the current semantic snapshot. "
            "Choose another available recovery mode, act from pageContent or semantic_refs, "
            "navigate from grounded evidence, finish, or explain that no usable target exists."
        ),
        "next_step": "use_an_available_recovery_tool_or_stop",
    }, ensure_ascii=False)


def _reset_chrome_exploration_budget(
    state: dict[str, Any],
    *,
    latest_snapshot_id: str | None,
) -> None:
    if latest_snapshot_id:
        state["latest_snapshot_id"] = latest_snapshot_id
    else:
        state.pop("latest_snapshot_id", None)
    state["same_snapshot_read_count"] = 0
    state.setdefault("screenshotted_snapshot_ids", [])
    state["waits_since_state_change"] = 0
    state["no_progress_recovery_count"] = 0
    state.pop("post_action_read_credit", None)


def _record_chrome_snapshot(
    state: dict[str, Any],
    *,
    snapshot_id: str,
    from_read_page: bool,
) -> None:
    if not snapshot_id:
        return
    unique_snapshot_ids = [
        str(value).strip()
        for value in state.get("unique_snapshot_ids", [])
        if str(value or "").strip()
    ]
    if snapshot_id not in unique_snapshot_ids:
        unique_snapshot_ids.append(snapshot_id)
        state["unique_snapshot_ids"] = unique_snapshot_ids[-50:]
    previous_snapshot_id = str(state.get("latest_snapshot_id") or "").strip()
    if snapshot_id != previous_snapshot_id:
        _reset_chrome_exploration_budget(state, latest_snapshot_id=snapshot_id)
        state["same_snapshot_read_count"] = 1 if from_read_page else 0
        return
    if from_read_page:
        state["same_snapshot_read_count"] = int(state.get("same_snapshot_read_count") or 0) + 1
        if state["same_snapshot_read_count"] > 1:
            state["no_progress_recovery_count"] = int(state.get("no_progress_recovery_count") or 0) + 1


def _record_chrome_screenshot(state: dict[str, Any]) -> None:
    snapshot_id = str(state.get("latest_snapshot_id") or "").strip()
    if not snapshot_id:
        return
    screenshots = [
        str(value).strip()
        for value in state.get("screenshotted_snapshot_ids", [])
        if str(value or "").strip()
    ]
    if snapshot_id not in screenshots:
        screenshots.append(snapshot_id)
        state["screenshotted_snapshot_ids"] = screenshots[-20:]
        state["no_progress_recovery_count"] = int(state.get("no_progress_recovery_count") or 0) + 1


def _workflow_scene_capture_context(arguments: dict[str, Any] | None) -> bool:
    args = arguments if isinstance(arguments, dict) else {}
    return bool(
        str(args.get("_workflow_action_grant_id_from_context") or "").strip()
        and (
            str(args.get("_workflow_scene_id_from_context") or "").strip()
            or str(args.get("_workflow_batch_capture_from_context") or "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
    )


def _capture_receipt_ids(state: dict[str, Any], key: str) -> list[str]:
    return [
        str(value).strip()
        for value in state.get(key, [])
        if str(value or "").strip()
    ]


def _record_workflow_scene_capture_receipt(
    state: dict[str, Any],
    *,
    canonical_tool: str,
    payload: dict[str, Any],
) -> None:
    document_id = str(payload.get("document_id") or "").strip()
    if not document_id:
        return
    receipt_key = ""
    if (
        canonical_tool == "mcp__chrome__screenshot"
        and payload.get("artifact_ready") is True
        and str(payload.get("screenshot_path") or payload.get("knowledge_path") or "").strip()
    ):
        receipt_key = "ready_screenshot_document_ids"
    elif (
        canonical_tool in {
            "mcp__chrome__stop_tab_recording",
            "mcp__chrome__get_tab_recording",
        }
        and str(payload.get("status") or "").strip() == "completed"
        and str(
            payload.get("knowledge_path")
            or payload.get("fs_path")
            or payload.get("result_url")
            or payload.get("clip_path")
            or ""
        ).strip()
    ):
        receipt_key = "ready_recording_document_ids"
    if not receipt_key:
        return
    receipt_ids = _capture_receipt_ids(state, receipt_key)
    if document_id not in receipt_ids:
        receipt_ids.append(document_id)
        state[receipt_key] = receipt_ids[-20:]


def _record_chrome_wait(state: dict[str, Any]) -> None:
    if not str(state.get("latest_snapshot_id") or "").strip():
        return
    state["waits_since_state_change"] = int(state.get("waits_since_state_change") or 0) + 1
    state["no_progress_recovery_count"] = int(state.get("no_progress_recovery_count") or 0) + 1


def runtime_chrome_workflow_summary(runtime_metadata: dict[str, Any] | None) -> dict[str, int]:
    raw_state = (runtime_metadata or {}).get(_CHROME_RUNTIME_CONTRACT_STATE_KEY)
    state = raw_state if isinstance(raw_state, dict) else {}
    unique_snapshot_ids = {
        str(value).strip()
        for value in state.get("unique_snapshot_ids", [])
        if str(value or "").strip()
    }
    return {
        "chrome_call_count": int(state.get("chrome_call_count") or 0),
        "read_page_call_count": int(state.get("read_page_call_count") or 0),
        "screenshot_call_count": int(state.get("screenshot_call_count") or 0),
        "wait_call_count": int(state.get("wait_call_count") or 0),
        "unique_snapshot_count": len(unique_snapshot_ids),
        "no_progress_block_count": int(state.get("no_progress_block_count") or 0),
    }


def runtime_blocked_chrome_action_shortcut(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    active_user_message: str | None,
) -> str | None:
    """Block Chrome actions that skip the mcp-chrome read_page/ref loop."""
    if not detect_chrome_local_browser_route(active_user_message):
        return None
    args = arguments or {}
    fallback_reason = str(args.get("fallbackReason") or args.get("fallback_reason") or "").strip()

    if tool_name == "mcp__chrome__type_text":
        if not fallback_reason:
            return _blocked_chrome_fallback_required(
                tool_name,
                "Chrome type_text inserts text into the currently focused element and cannot identify the target by itself.",
            )

    if tool_name in _CHROME_NODE_ACTION_TOOLS:
        ref = str(args.get("ref") or args.get("node_id") or args.get("nodeId") or "").strip()
        if not _CHROME_REF_RE.match(ref) and not fallback_reason:
            return _blocked_invalid_chrome_ref(tool_name, "ref", ref)

    if tool_name == "mcp__chrome__click_point" and not fallback_reason:
        return _blocked_chrome_fallback_required(
            tool_name,
            "Chrome click_point clicks a viewport coordinate without knowing the semantic page target.",
        )

    if tool_name == "mcp__chrome__computer":
        action = str(args.get("action") or "").strip()
        if action in {"screenshot", "wait"}:
            return None
        ref = str(args.get("ref") or args.get("node_id") or args.get("nodeId") or "").strip()
        if _CHROME_REF_RE.match(ref):
            return None
        if not fallback_reason:
            return _blocked_chrome_fallback_required(
                tool_name,
                f"Chrome computer action '{action or 'unknown'}' is a low-level visual/focused fallback without a read_page ref.",
            )

    if tool_name == "mcp__chrome__claim_tab":
        claim_token = str(args.get("claimToken") or args.get("claim_token") or "").strip()
        if not claim_token:
            return json.dumps({
                "status": "blocked",
                "reason": "chrome_claim_token_required",
                "blocked_tool": tool_name,
                "message": (
                    "Chrome claim_tab requires a claimToken returned by "
                    "mcp__chrome__open_tabs or mcp__chrome__list_tabs. Do not guess tab ids."
                ),
                "next_required_tool": "mcp__chrome__open_tabs",
            }, ensure_ascii=False)

    if tool_name in {"mcp__chrome__scroll", "mcp__chrome__scroll_wheel"}:
        delta_x = _number_arg(args, "deltaX")
        delta_y = _number_arg(args, "deltaY")
        direction = str(args.get("direction") or "").strip()
        if not direction and delta_x == 0 and delta_y == 0:
            return json.dumps({
                "status": "blocked",
                "reason": "chrome_no_op_scroll",
                "blocked_tool": tool_name,
                "message": "Chrome scroll with zero delta does not change page state and only moves the cursor.",
                "next_step": (
                    "Use mcp__chrome__read_page if you need element refs, "
                    "or scroll with a meaningful non-zero delta and then call "
                    "mcp__chrome__read_page once."
                ),
            }, ensure_ascii=False)

    if tool_name in {"mcp__chrome__press_key", "mcp__chrome__keyboard"}:
        key = str(args.get("key") or args.get("keys") or "").strip()
        if not _chrome_key_supported(key):
            return json.dumps({
                "status": "blocked",
                "reason": "chrome_unsupported_key",
                "blocked_tool": tool_name,
                "message": f"Unsupported Chrome key: {key}",
                "next_step": (
                    "Use a supported single key such as Enter, Tab, Escape, "
                    "Home, End, PageUp, PageDown, Backspace, Delete, Space, "
                    "or ArrowUp/ArrowDown/ArrowLeft/ArrowRight. For navigation, "
                    "read the page with mcp__chrome__read_page and click a "
                    "visible ref instead of sending shortcut chords."
                ),
            }, ensure_ascii=False)
        if not fallback_reason:
            return _blocked_chrome_fallback_required(
                tool_name,
                "Chrome key tools send a key to the currently focused element and need an explicit focus/fallback reason.",
            )

    return None


def _blocked_chrome_action_before_read_page(
    *,
    canonical_tool: str,
    arguments: dict[str, Any],
    state: dict[str, Any],
) -> str | None:
    if canonical_tool == "mcp__chrome__read_page":
        return None
    pending = state.get("pending_read_page_required_action")
    if not isinstance(pending, dict):
        return None
    if canonical_tool not in _CHROME_PAGE_MUTATION_TOOLS:
        return None
    return json.dumps({
        "status": "blocked",
        "reason": "chrome_read_page_required_before_retry",
        "blocked_tool": canonical_tool,
        "previous_reason": pending.get("reason") or "read_page_required",
        "message": (
            "The previous Chrome action required a fresh mcp__chrome__read_page. "
            "Do not perform another ref, selector, coordinate, keyboard, focused typing, or navigation action before a successful fresh read."
        ),
        "next_required_tool": "mcp__chrome__read_page",
    }, ensure_ascii=False)


def _blocked_chrome_action_before_observation(
    *,
    canonical_tool: str,
    state: dict[str, Any],
) -> str | None:
    if canonical_tool in _CHROME_OBSERVATION_AFTER_ACTION_ALLOWED_TOOLS:
        return None
    if canonical_tool not in _CHROME_PAGE_MUTATION_TOOLS:
        return None
    pending = state.get("pending_observation_after_action")
    if not isinstance(pending, dict):
        return None
    return json.dumps({
        "status": "blocked",
        "reason": "chrome_observation_required_after_action",
        "blocked_tool": canonical_tool,
        "previous_tool": pending.get("tool_name"),
        "message": (
            "A previous Chrome action may have changed the page. Call mcp__chrome__wait if needed, "
            "then mcp__chrome__read_page before taking another page-changing action."
        ),
        "next_required_tool": "mcp__chrome__read_page",
        "optional_before_read_tool": "mcp__chrome__wait",
    }, ensure_ascii=False)


def _chrome_tool_result_requires_observation(tool_name: str, payload: dict[str, Any]) -> bool:
    if tool_name not in _CHROME_PAGE_MUTATION_TOOLS:
        return False
    if payload.get("ok") is False:
        return False
    status = str(payload.get("status") or "").strip()
    if status in {
        "read_page_required",
        "approval_required",
        "reused_noop",
        "listed",
        "current",
        "none",
    }:
        return False
    return True


def _chrome_result_has_verified_post_action_state(
    tool_name: str,
    payload: dict[str, Any],
    arguments: dict[str, Any] | None = None,
) -> bool:
    if tool_name not in _CHROME_TRUSTED_POST_ACTION_TOOLS:
        return False
    if payload.get("ok") is not True:
        return False
    state = payload.get("post_action_page_state")
    if not isinstance(state, dict):
        return False
    target_tab_ids = {
        payload.get("tabId"),
        payload.get("target_tab_id"),
    } - {None}
    snapshot_id = str(state.get("snapshot_id") or "").strip()
    previous_snapshot_ids = {
        str(value).strip()
        for value in (
            (arguments or {}).get("snapshot_id"),
            (arguments or {}).get("snapshotId"),
            payload.get("snapshot_id"),
            payload.get("snapshotId"),
        )
        if str(value or "").strip()
    }
    return (
        state.get("ok") is True
        and state.get("state_verified") is True
        and bool(snapshot_id)
        and snapshot_id not in previous_snapshot_ids
        and bool(target_tab_ids)
        and state.get("tabId") in target_tab_ids
    )


def _canonical_chrome_tool_name(tool_name: str) -> str:
    name = str(tool_name or "").strip()
    if name.startswith("mcp__chrome__"):
        return name
    if name.startswith("browser_"):
        name = name[len("browser_"):]
    if name:
        return f"mcp__chrome__{name}"
    return name


def _chrome_runtime_contract_state(runtime_metadata: dict[str, Any]) -> dict[str, Any]:
    raw = runtime_metadata.setdefault(_CHROME_RUNTIME_CONTRACT_STATE_KEY, {})
    if isinstance(raw, dict):
        return raw
    replacement: dict[str, Any] = {}
    runtime_metadata[_CHROME_RUNTIME_CONTRACT_STATE_KEY] = replacement
    return replacement


def _chrome_action_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    significant = {
        key: arguments.get(key)
        for key in sorted(arguments)
        if not _chrome_action_signature_ignored_key(key)
    }
    payload = json.dumps(
        {"tool": tool_name, "arguments": significant},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _chrome_action_signature_ignored_key(key: str) -> bool:
    if key in {
        "approvalToken",
        "approval_token",
        "turnId",
        "turn_id",
        "sessionId",
        "session_id",
        "task_id",
        "taskId",
        "thread_id",
        "threadId",
        "workspace_id",
        "workspaceId",
        "conversation_id",
        "conversationId",
    }:
        return True
    return key.startswith("_") and key.endswith("_from_context")


def _parse_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _blocked_chrome_fallback_required(tool_name: str, detail: str) -> str:
    return json.dumps({
        "status": "blocked",
        "reason": "chrome_fallback_reason_required",
        "blocked_tool": tool_name,
        "message": (
            f"{detail} Use mcp__chrome__read_page first and prefer a ref plus "
            "snapshot_id from chrome_read_page/read_page; use the ref from chrome_read_page. For text entry or form input, use "
            "mcp__chrome__fill_or_select on the visible ref. Selector, "
            "coordinate, keyboard, or focused typing fallback requires a "
            "concrete fallbackReason explaining why read_page could not provide "
            "a usable target or why focus is known."
        ),
        "next_required_tool": "mcp__chrome__read_page",
    }, ensure_ascii=False)


def _blocked_invalid_chrome_ref(tool_name: str, field: str, value: str) -> str:
    return json.dumps({
        "status": "blocked",
        "reason": "chrome_invalid_read_page_ref",
        "blocked_tool": tool_name,
        field: value,
        "message": (
            f"Chrome {field} must be a concrete target from the latest "
            "mcp__chrome__read_page result for the same tabId. Do not invent "
            f"{field} values."
        ),
        "next_step": (
            "Call mcp__chrome__read_page to understand the current page and get "
            "valid refs like e9. "
            "Choose a target whose label/role matches the requested action."
        ),
    }, ensure_ascii=False)


def _number_arg(arguments: dict[str, Any], key: str) -> float:
    value = arguments.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _chrome_key_supported(key: str) -> bool:
    if not key:
        return False
    if "+" in key:
        return False
    if re.fullmatch(r"F\d{1,2}", key, re.IGNORECASE):
        return False
    return key in _CHROME_SUPPORTED_KEY_NAMES or len(key) == 1
