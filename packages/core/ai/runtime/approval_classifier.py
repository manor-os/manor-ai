from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from packages.core.ai.runtime.approvals import RuntimeApprovalAction
from packages.core.ai.runtime.bash_command_specs import BashCommandSpecFactory
from packages.core.ai.runtime.chrome_tool_catalog import (
    CHROME_MCP_TOOLS,
    CHROME_READ_ACTIONS,
    CHROME_READ_ONLY_CDP_METHODS,
    canonical_chrome_tool_name,
    chrome_key_sequence,
    chrome_navigation_key,
    normalize_chrome_call,
)
from packages.core.ai.runtime.tool_effect_classification import (
    RuntimeToolClassification,
    RuntimeToolClassificationRequest,
    RuntimeToolClassificationStrategy,
    RuntimeToolAuthorization,
    RuntimeToolClassifier,
    RuntimeToolClassifierFactory,
    RuntimeToolEffect,
    RuntimeWorkspaceFileScope,
    WorkspaceFileExistence,
)
from packages.core.services.runtime_authorization.domain import (
    RuntimeAuthorizationAccess,
)
from packages.core.services.official_remote_mcp import (
    MCPActionEffect,
    OfficialRemoteMCPActionPolicyFactory,
    OfficialRemoteMCPFactory,
    OfficialRemoteMCPProvider,
)


__all__ = [
    "bash_write_targets",
    "ChromeConfirmationDecision",
    "classify_chrome_confirmation",
    "is_chrome_basic_call",
    "classify_runtime_tool",
    "classify_runtime_tool_action",
    "RuntimeToolClassification",
    "RuntimeToolClassificationRequest",
    "RuntimeToolClassificationStrategy",
    "RuntimeToolClassifier",
    "RuntimeToolClassifierFactory",
    "RuntimeToolEffect",
    "split_mcp_tool",
]


@dataclass(frozen=True)
class ChromeConfirmationDecision:
    mode: str
    policy_category: str
    preapproved: bool = False
    destination: str = ""
    data_summary: str = ""


@dataclass(frozen=True)
class ChromeCallDecision:
    kind: str
    tool_name: str
    confirmation: ChromeConfirmationDecision | None = None
    reason: str = ""


_CHROME_ACTION_INTENT_RE = re.compile(
    r"\b(upload|attach|allow|permit|login|log in|sign in|submit|send|share|enter|provide)\b|"
    r"上传|附加|允许|授权|登录|提交|发送|分享|填写|提供",
    re.IGNORECASE,
)
_CHROME_PERMISSION_TERMS = {
    "camera": ("camera", "webcam", "摄像头", "相机"),
    "microphone": ("microphone", "mic", "麦克风"),
    "location": ("location", "geolocation", "位置", "定位"),
    "notification": ("notification", "notifications", "通知"),
    "clipboard": ("clipboard", "剪贴板"),
}
_CHROME_SENSITIVE_TERMS = {
    "password": ("password", "passcode", "passphrase", "密码"),
    "authentication_code": ("one-time-code", "otp", "totp", "mfa", "2fa", "verification code", "auth code", "验证码", "动态码"),
    "secret": ("api key", "secret", "token", "密钥", "令牌"),
    "payment": ("cvv", "cvc", "card number", "credit card", "cc-number", "payment", "银行卡", "信用卡", "安全码"),
    "passport": ("passport", "护照"),
    "identity": ("ssn", "social security", "national id", "tax id", "身份证", "税号"),
    "bank_account": ("routing number", "account number", "银行账号", "银行账户"),
}
_CHROME_PAGE_EFFECT_ACTIONS = {
    "click",
    "click_element",
    "click_point",
    "fill",
    "fill_or_select",
    "type_text",
    "press_key",
    "press",
    "key",
    "keyboard",
    "computer",
    "js_dialog",
    "handle_js_dialog",
    "send_cdp",
    "inject_script",
}
_CHROME_BASIC_MUTATION_ACTIONS = frozenset({
    "open", "open_new_tab", "open_or_reuse", "name_session", "navigate",
    "open_tabs", "back", "forward", "reload", "claim_tab", "finalize_tabs", "hover",
    "fill_or_select", "scroll", "scroll_wheel", "click_element",
    "press_key", "keyboard", "type_text", "click_point",
    "close_tab", "close_tabs", "close_group_tabs", "set_cursor", "hide_cursor", "set_badge",
    "download", "drag", "start_tab_recording", "switch_tab_recording_source",
    "stop_tab_recording", "cancel_tab_recording",
})
_CHROME_EXTERNAL_COMMIT_RE = re.compile(
    r"^(?:(?:publish|publicar|post|send|submit|upload|delete|remove|buy|purchase|checkout|pay|allow|share|verify|schedule)(?:\b|$)|"
    r"(?:立即|确认|定时|计划|安排)?(?:发布|发表|发送|提交|上传|删除|移除|购买|结账|支付|允许|分享|验证))",
    re.IGNORECASE,
)


def classify_chrome_confirmation(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    initial_user_message: str | None = None,
) -> ChromeConfirmationDecision:
    args = arguments or {}
    action = _chrome_action_name(tool_name)
    target = args.get("target")
    target_values = (
        tuple(target.get(key) for key in ("label", "aria_label", "name", "text"))
        if isinstance(target, dict) else ()
    )
    label = " ".join(
        value
        for value in (
            *(str(args.get(key) or "").strip()
              for key in ("label", "target_label", "aria_label", "name")),
            *(str(value or "").strip() for value in target_values),
        )
        if value
    )
    url = str(args.get("url") or args.get("target_url") or "").strip()
    destination = _chrome_destination(url)
    haystack = f"{label} {url}".strip().lower()

    if action in {"download", "wait_download"}:
        return ChromeConfirmationDecision("no_confirmation", "inbound_download", destination=destination)
    if action == "history":
        return ChromeConfirmationDecision("always_action_time", "browser_history", destination=destination)
    if action == "clipboard_write":
        return ChromeConfirmationDecision("always_action_time", "browser_system_setting", destination=destination)
    if action in {"upload", "set_files", "upload_ref"}:
        filenames = _chrome_upload_filenames(args)
        preapproved = bool(filenames) and _chrome_specific_preapproval(
            initial_user_message,
            destination=destination,
            required_terms=filenames,
        )
        return ChromeConfirmationDecision(
            "preapproval_allowed",
            "file_upload",
            preapproved=preapproved,
            destination=destination,
            data_summary=", ".join(filenames[:3]),
        )
    explicitly_requested = any(
        args.get(key) is True
        for key in ("requiresApproval", "requires_approval", "sideEffect", "side_effect", "sensitive")
    )
    if action not in _CHROME_PAGE_EFFECT_ACTIONS and not explicitly_requested:
        return ChromeConfirmationDecision("no_confirmation", "ordinary_page_action", destination=destination)
    if re.search(r"\b(accept|allow|agree|ok)\b.*\b(cookie|cookies)\b|接受.*Cookie|同意.*Cookie", haystack, re.IGNORECASE):
        return ChromeConfirmationDecision("no_confirmation", "cookie_consent", destination=destination)
    if _chrome_password_change_submit(action, haystack, args):
        return ChromeConfirmationDecision("handoff_required", "password_change", destination=destination)
    if args.get("unsupported_automation") is True or args.get("handoff_required") is True:
        return ChromeConfirmationDecision("handoff_required", "unsupported_automation", destination=destination)

    sensitive_category = _chrome_sensitive_category(haystack)
    if isinstance(target, dict):
        autocomplete = set(str(target.get("autocomplete") or "").lower().split())
        if str(target.get("type") or "").lower() == "password" or autocomplete & {"current-password", "new-password"}:
            sensitive_category = "password"
        elif "one-time-code" in autocomplete:
            sensitive_category = "authentication_code"
        elif any(token.startswith("cc-") for token in autocomplete):
            sensitive_category = "payment"
    role = str(target.get("role") or "").lower() if isinstance(target, dict) else ""
    editing = action in {"fill_or_select", "type_text"} or (
        action in {"click_element", "click_point", "keyboard", "press_key"}
        and role in {"checkbox", "radio", "switch", "option", "combobox", "listbox"}
    )
    if editing and not sensitive_category and not explicitly_requested:
        return ChromeConfirmationDecision("no_confirmation", "ordinary_page_action", destination=destination)
    if isinstance(target, dict) and target.get("activation") in {"navigation", "popup"} and not explicitly_requested and not editing:
        return ChromeConfirmationDecision("no_confirmation", "ordinary_page_action", destination=destination)
    if sensitive_category == "captcha":
        return ChromeConfirmationDecision("always_action_time", "captcha", destination=destination, data_summary="redacted captcha input")
    if re.search(r"\b(delete|remove|erase|revoke|unsubscribe)\b|删除|移除|撤销|注销", haystack, re.IGNORECASE):
        return ChromeConfirmationDecision("always_action_time", "deletion", destination=destination)
    if re.search(r"\b(pay|purchase|buy|checkout|place order|confirm order)\b|支付|购买|结账|下单", haystack, re.IGNORECASE):
        return ChromeConfirmationDecision("always_action_time", "financial_transaction", destination=destination)

    permission_kind = _chrome_permission_kind(haystack)
    if re.search(r"\b(allow|permit|grant)\b|允许|授权", haystack, re.IGNORECASE) and permission_kind:
        preapproved = _chrome_specific_preapproval(
            initial_user_message,
            destination=destination,
            required_terms=_CHROME_PERMISSION_TERMS[permission_kind],
            match_any_term=True,
        )
        return ChromeConfirmationDecision(
            "preapproval_allowed",
            "browser_permission",
            preapproved=preapproved,
            destination=destination,
            data_summary=f"{permission_kind} permission",
        )

    if sensitive_category:
        terms = _CHROME_SENSITIVE_TERMS.get(sensitive_category, ())
        preapproved = _chrome_specific_preapproval(
            initial_user_message,
            destination=destination,
            required_terms=terms,
            match_any_term=True,
        )
        return ChromeConfirmationDecision(
            "preapproval_allowed",
            "sensitive_data_transmission",
            preapproved=preapproved,
            destination=destination,
            data_summary=f"redacted {sensitive_category.replace('_', ' ')} data",
        )

    if isinstance(target, dict) and (target.get("activation") == "form_submit" or target.get("submit_candidate") is True):
        return ChromeConfirmationDecision("always_action_time", "representational_communication", destination=destination)
    if _CHROME_EXTERNAL_COMMIT_RE.search(label):
        return ChromeConfirmationDecision("always_action_time", "representational_communication", destination=destination)
    if re.search(r"\b(subscribe|enable notifications)\b|订阅|开启通知", haystack, re.IGNORECASE):
        return ChromeConfirmationDecision("always_action_time", "notification_subscription", destination=destination)
    if re.search(r"\b(install|execute|run downloaded)\b|安装|执行下载", haystack, re.IGNORECASE):
        return ChromeConfirmationDecision("always_action_time", "software_installation", destination=destination)
    if re.search(r"\b(login|log in|sign in|age verification|verify age)\b|登录|年龄验证|验证年龄", haystack, re.IGNORECASE):
        category = "age_verification" if re.search(r"age|年龄", haystack, re.IGNORECASE) else "authentication"
        return ChromeConfirmationDecision(
            "preapproval_allowed",
            category,
            preapproved=_chrome_specific_preapproval(initial_user_message, destination=destination),
            destination=destination,
        )
    if explicitly_requested:
        return ChromeConfirmationDecision("always_action_time", "explicit_side_effect", destination=destination)
    return ChromeConfirmationDecision("no_confirmation", "ordinary_page_action", destination=destination)


def is_chrome_basic_call(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> bool:
    """Whether a known Chrome call is safe to treat as ordinary browser work."""
    if tool_name == "search_tools":
        return _is_verified_chrome_tool_discovery(arguments or {})
    return classify_chrome_call(tool_name, arguments or {}).kind in {"read", "ordinary"}


def classify_chrome_call(tool_name: str, arguments: dict[str, Any]) -> ChromeCallDecision:
    """Classify a known operation once; missing evidence is not an approval."""
    known_tool = canonical_chrome_tool_name(tool_name) in CHROME_MCP_TOOLS
    tool_name, args = normalize_chrome_call(tool_name, arguments)
    action = _chrome_action_name(tool_name)
    if not known_tool or action == "computer":
        return ChromeCallDecision("unsupported", tool_name, reason="chrome_unsupported_action")
    intent = args.get("operation_intent")
    effect = str(intent.get("effect") or "").strip().lower() if isinstance(intent, dict) else ""
    if effect == "publish":
        return ChromeCallDecision("confirmation", tool_name, ChromeConfirmationDecision("always_action_time", "publication"))
    if effect not in {"", "prepare", "read", "draft", "navigation", "interact"}:
        return ChromeCallDecision("unsupported", tool_name, reason="chrome_unsupported_effect")
    if action == "send_cdp":
        if isinstance(args.get("method"), str) and args["method"] in CHROME_READ_ONLY_CDP_METHODS:
            return ChromeCallDecision("read", tool_name)
        return ChromeCallDecision("unsupported", tool_name, reason="chrome_unsupported_cdp_method")
    if action in CHROME_READ_ACTIONS:
        return ChromeCallDecision("read", tool_name)
    if action in {"clipboard_read", "get_tab_recording", "wait_download"}:
        return ChromeCallDecision("read", tool_name)
    if action == "inject_script":
        return ChromeCallDecision("confirmation", tool_name, ChromeConfirmationDecision("always_action_time", "script_execution"))
    if action == "download" and not args.get("url"):
        return classify_chrome_call("mcp__chrome__click_element", args)
    if action == "handle_js_dialog":
        if args.get("accept") is False:
            return ChromeCallDecision("ordinary", tool_name)
        return ChromeCallDecision("confirmation", tool_name, ChromeConfirmationDecision("always_action_time", "browser_dialog"))
    if action == "execute_cached_step":
        child = args.get("_runtime_trusted_browser_cached_action")
        if isinstance(child, str) and child in {"click_element", "fill_or_select"}:
            return classify_chrome_call("mcp__chrome__" + child, args)
        return ChromeCallDecision("observation_required", tool_name, reason="chrome_cached_target_observation_required")
    if action in {"click_element", "click_point", "fill_or_select", "type_text", "press_key", "keyboard"}:
        navigation = action in {"press_key", "keyboard"} and chrome_navigation_key(args)
        if navigation and not any(args.get(key) is True for key in (
            "requiresApproval", "requires_approval", "sideEffect", "side_effect", "sensitive",
        )):
            return ChromeCallDecision("ordinary", tool_name)
        target = args.get("target")
        if action in {"click_point", "type_text", "press_key", "keyboard"} and not navigation and not isinstance(target, dict):
            return ChromeCallDecision("observation_required", tool_name, reason="chrome_target_observation_required")
        if not navigation and not _chrome_action_has_semantic_target(args):
            return ChromeCallDecision("observation_required", tool_name, reason="chrome_target_observation_required")
        if action in {"press_key", "keyboard"} and not chrome_key_sequence(args):
            return ChromeCallDecision("unsupported", tool_name, reason="chrome_unsupported_key")
        if action in {"press_key", "keyboard"} and chrome_key_sequence(args)[-1:] in {("enter",), ("return",)}:
            if isinstance(target, dict) and target.get("role") in {"textbox", "searchbox"} and target.get("tag") != "textarea":
                return ChromeCallDecision("observation_required", tool_name, reason="chrome_explicit_submit_target_required")
        if isinstance(target, dict) and (target.get("disabled") is True or (
            action in {"fill_or_select", "type_text"} and target.get("read_only") is True
        )):
            return ChromeCallDecision("observation_required", tool_name, reason="chrome_target_not_editable")
    decision = classify_chrome_confirmation(tool_name, args)
    if decision.mode == "handoff_required":
        return ChromeCallDecision("handoff", tool_name, decision)
    if decision.mode != "no_confirmation":
        return ChromeCallDecision("confirmation", tool_name, decision)
    if action in _CHROME_BASIC_MUTATION_ACTIONS:
        return ChromeCallDecision("ordinary", tool_name)
    # These capabilities have their own effect or execution contract. An
    # unmodeled call must never turn into ordinary_page_action.confirm.
    return ChromeCallDecision("unsupported", tool_name, reason="chrome_action_contract_required")


def _chrome_action_has_semantic_target(args: dict[str, Any]) -> bool:
    target = args.get("target")
    if isinstance(target, dict) and (
        target.get("editable") is True
        or str(target.get("role") or "").lower() in {"textbox", "combobox", "checkbox", "radio", "slider", "spinbutton", "switch"}
    ):
        return True
    if any(str(args.get(key) or "").strip() for key in ("label", "target_label", "aria_label", "name")):
        return True
    target = args.get("target")
    return isinstance(target, dict) and any(
        str(target.get(key) or "").strip()
        for key in ("label", "aria_label", "name", "text", "testId", "href")
    )


def _chrome_action_name(tool_name: str) -> str:
    name = str(tool_name or "").strip()
    if name.startswith("mcp__"):
        server, action = split_mcp_tool(name)
        return action if server == "chrome" else ""
    for prefix in ("chrome_", "browser_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _chrome_destination(url: str) -> str:
    try:
        return str(urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _chrome_upload_filenames(args: dict[str, Any]) -> tuple[str, ...]:
    raw = args.get("files") or args.get("paths") or []
    values = raw if isinstance(raw, list) else [raw]
    filenames: list[str] = []
    for value in values:
        filename = os.path.basename(str(value or "").replace("\\", "/")).strip()
        if filename and filename not in filenames:
            filenames.append(filename)
    return tuple(filenames[:8])


def _chrome_specific_preapproval(
    initial_user_message: str | None,
    *,
    destination: str,
    required_terms: tuple[str, ...] = (),
    match_any_term: bool = False,
) -> bool:
    message = str(initial_user_message or "").strip().lower()
    if not message or not destination or destination not in message:
        return False
    if not _CHROME_ACTION_INTENT_RE.search(message):
        return False
    terms = [str(term).lower() for term in required_terms if str(term).strip()]
    if not terms:
        return True
    if match_any_term:
        return any(term in message for term in terms)
    return all(term in message for term in terms)


def _chrome_permission_kind(haystack: str) -> str:
    for kind, terms in _CHROME_PERMISSION_TERMS.items():
        if any(term.lower() in haystack for term in terms):
            return kind
    return ""


def _chrome_sensitive_category(haystack: str) -> str:
    if re.search(r"\bcaptcha\b|人机验证", haystack, re.IGNORECASE):
        return "captcha"
    for category, terms in _CHROME_SENSITIVE_TERMS.items():
        if any(term.lower() in haystack for term in terms):
            return category
    return ""


def _chrome_password_change_submit(action: str, haystack: str, args: dict[str, Any]) -> bool:
    if action not in {"click", "click_element", "click_point", "press_key", "keyboard", "computer"}:
        return False
    if args.get("password_change") is True:
        return True
    return bool(
        re.search(r"change password|update password|save password|reset password|修改密码|更改密码|重置密码", haystack, re.IGNORECASE)
    )


_SOCIAL_PUBLISH_ACTIONS = {
    "create_tweet",
    "comment_tweet",
    "create_thread",
    "post_tweet",
    "create_post",
    "create_multi_photo_post",
    "create_video_post",
    "publish_instagram_media",
    "create_comment",
    "comment_note",
    "reply_comment",
    "reply_instagram_comment",
}
_SOCIAL_MUTATION_ACTIONS = {
    "update_post",
    "delete_post",
    "delete_tweet",
    "like_note",
    "unlike_note",
    "like_tweet",
    "unlike_tweet",
    "retweet",
    "unretweet",
    "follow_user",
    "unfollow_user",
    "delete_comment",
    "react_to_post",
    "remove_reaction",
    "delete_instagram_comment",
}
_EMAIL_SEND_ACTIONS = {
    "send_message",
    "send_draft",
    "send_email",
    "reply_to_message",
    "reply_all",
}
_MESSAGE_SEND_ACTIONS = {
    "send_text_message",
    "send_image_message",
    "send_template_message",
    "send_messenger",
    "send_messenger_image",
}
_DESTRUCTIVE_PREFIXES = ("delete_", "remove_", "revoke_", "refund_", "void_")
_MUTATION_PREFIXES = (
    "accept_",
    "add_",
    "adjust_",
    "archive_",
    "batch_",
    "cancel_",
    "clear_",
    "close_",
    "comment_",
    "copy_",
    "create_",
    "decline_",
    "delete_",
    "edit_",
    "empty_",
    "end_",
    "flag_",
    "follow_",
    "fork_",
    "forward_",
    "invite_",
    "like_",
    "mark_",
    "merge_",
    "move_",
    "patch_",
    "post_",
    "publish_",
    "push_",
    "put_",
    "quick_add_",
    "rate_",
    "react_",
    "remove_",
    "rename_",
    "reply_",
    "request_",
    "rerun_",
    "resolve_",
    "respond_",
    "restore_",
    "run_",
    "save_",
    "send_",
    "set_",
    "share_",
    "tentatively_",
    "trash_",
    "unfollow_",
    "unlike_",
    "unretweet_",
    "untrash_",
    "update_",
    "upload_",
    "write_",
)
_EXTERNAL_COMMIT_PREFIXES = (
    "comment_",
    "forward_",
    "post_",
    "publish_",
    "reply_",
    "respond_",
    "send_",
)
_EXTERNAL_COMMIT_ACTIONS = {
    "create_comment",
    "create_reply",
    "send",
}
_NON_REPRESENTATIONAL_EXTERNAL_ACTIONS = {
    "send_typing_indicator",
}
_MCP_AUTHORIZATION_CAPABILITY = "mcp.use_personal"
_OFFICIAL_REMOTE_MCP_DEFINITIONS = {
    provider.value: OfficialRemoteMCPFactory.create(provider)
    for provider in OfficialRemoteMCPProvider
}
_MCP_READ_ONLY_ACTIONS_BY_SERVER: dict[str, frozenset[str]] = {
    "apify": frozenset({"quote_instagram_collection", "get_instagram_collection"}),
    "alpaca_market_data": frozenset({
        "get_bars", "get_latest_quote", "get_latest_trade", "get_news", "get_option_chain",
        "get_option_latest_quote", "get_option_snapshot", "get_snapshot",
    }),
    "alpha_vantage": frozenset({
        "get_company_overview", "get_daily_series", "get_fundamentals", "get_quote", "search_symbols",
    }),
    "amazon": frozenset({
        "get_catalog_item", "get_inventory_summaries", "get_listing_item", "get_order",
        "get_order_items", "get_orders", "search_catalog_items",
    }),
    "chrome": frozenset({
        "capabilities", "clipboard_read", "console_logs", "documentation", "get_group_state",
        "get_interactive_elements", "get_tab_recording", "get_web_content", "get_windows_and_tabs",
        "inspect_selector", "list_tabs", "page_assets", "ping_tab", "read_page", "resolve_target",
        "screenshot", "selected_tab", "status", "tab_info", "viewport", "wait", "wait_download",
    }),
    "claude_code": frozenset({"check_path"}),
    "codex_cli": frozenset({"check_path"}),
    "elevenlabs": frozenset({"list_voices"}),
    "suno": frozenset({"get_generation", "get_usage"}),
    "email": frozenset({
        "download_attachment",
        "get_draft",
        "get_message",
        "get_thread",
        "list_attachments",
        "list_drafts",
        "list_folders",
        "list_messages",
        "list_threads",
    }),
    "facebook": frozenset({
        "get_instagram_account", "get_instagram_insights", "get_instagram_media", "get_live_video",
        "get_page", "get_page_insights", "get_post", "get_post_insights", "list_comments",
        "list_conversation_messages", "list_conversations", "list_instagram_accounts",
        "list_instagram_comments", "list_instagram_media", "list_live_videos", "list_page_albums",
        "list_pages", "list_posts",
    }),
    "github": frozenset({
        "compare_commits", "get_authenticated_user", "get_branch", "get_commit", "get_commit_status",
        "get_issue", "get_job_logs", "get_pr", "get_pr_diff", "get_user", "get_workflow_run",
        "list_branches", "list_check_runs", "list_collaborators", "list_commits", "list_files",
        "list_forks", "list_issue_comments", "list_issues", "list_pr_files", "list_pr_review_comments",
        "list_pr_reviews", "list_prs", "list_releases", "list_repos", "list_run_artifacts",
        "list_runs", "list_tags", "list_topics", "list_workflow_jobs", "list_workflows", "read_file",
        "repo_info", "search_code", "search_issues", "search_repos", "search_users",
    }),
    "gmail": frozenset({
        "download_attachment", "get_draft", "get_message", "get_profile", "get_thread", "list_drafts",
        "list_labels", "list_messages", "list_threads",
    }),
    "twilio": frozenset({"get_usage", "list_phone_numbers"}),
    "google_calendar": frozenset({
        "freebusy_query", "get_event", "list_calendars", "list_event_attendees",
        "list_event_instances", "list_events",
    }),
    "google_drive": frozenset({
        "get_about", "get_file", "get_revision", "list_comments", "list_files", "list_permissions",
        "list_revisions", "read_file", "search_files",
    }),
    "linkedin": frozenset({
        "get_my_posts", "get_organization", "get_post_comments", "get_post_reactions", "get_post_stats",
        "get_profile", "list_org_posts", "list_organizations",
    }),
    "lark": frozenset({
        "get_chat", "get_document", "get_document_content", "list_bitable_fields",
        "list_bitable_records", "list_bitable_tables", "list_chats", "list_messages",
    }),
    "manor_mcp_minutes": frozenset({
        "chat_with_meeting", "get_action_items", "get_meeting_details", "get_meeting_stats",
        "get_summary", "get_transcript", "list_recent_meetings", "search_meetings",
    }),
    "manor_mcp_admin": frozenset({
        "get_discovery_health", "get_economics_report", "get_efficiency_metrics",
        "get_platform_overview", "get_reliability_metrics", "get_system_health", "get_system_metrics",
        "get_tenant", "get_tool_usage", "get_traffic_report", "get_usage_report", "list_announcements",
        "list_tenant_users", "list_tenants", "query_audit_log",
    }),
    "manor_mcp_calendar": frozenset({
        "get_calendar_settings", "get_daily_agenda", "list_booking_links", "list_bookings",
    }),
    "ms_calendar": frozenset({
        "find_meeting_times", "get_event", "get_schedule", "list_calendars", "list_event_instances",
        "list_events",
    }),
    "ms_excel": frozenset({
        "get_named_item_range", "get_table_rows", "list_named_items", "list_tables", "list_worksheets",
        "read_range", "read_used_range",
    }),
    "notion": frozenset({"get_page", "query_database", "search"}),
    "ms_teams": frozenset({
        "get_channel", "get_chat", "get_my_presence", "get_online_meeting",
        "list_channel_message_replies", "list_channel_messages", "list_channels", "list_chat_messages",
        "list_chats", "list_my_teams",
    }),
    "nango": frozenset({"nango_list_connections", "nango_list_providers"}),
    "onedrive": frozenset({
        "get_drive_info", "get_file", "get_file_by_path", "get_recent_files", "get_shared_with_me",
        "list_files", "list_permissions", "list_versions", "read_file", "search_files",
    }),
    "outlook": frozenset({
        "download_attachment", "get_message", "get_profile", "list_attachments", "list_folders",
        "list_messages",
    }),
    "paypal": _OFFICIAL_REMOTE_MCP_DEFINITIONS["paypal"].read_only_actions,
    "producthunt": frozenset({"daily_posts", "get_post", "list_comments", "me", "search_posts"}),
    "quickbooks": frozenset({
        "custom_query", "get_company_info", "get_customer", "get_invoice", "get_payment",
        "query_accounts", "query_bills", "query_customers", "query_invoices", "query_items",
        "query_payments", "query_vendors",
    }),
    "shopify": frozenset({
        "get_order", "get_product", "get_shop", "list_customers", "list_orders", "list_products",
    }),
    "square": frozenset({
        "get_catalog_object", "get_customer", "get_inventory", "get_order", "list_customers",
        "list_locations", "search_catalog_items", "search_orders",
    }),
    "stripe": _OFFICIAL_REMOTE_MCP_DEFINITIONS["stripe"].read_only_actions,
    "tavily": frozenset({"extract", "search"}),
    "telegram": frozenset({"get_me"}),
    "tiktok": frozenset({
        "get_creator_info", "get_publish_status", "get_user_info", "list_videos", "query_videos",
    }),
    "tiktok_shop": frozenset({
        "get_authorized_shops", "get_order_detail", "get_product", "search_orders", "search_products",
    }),
    "twelve_data": frozenset({"get_price", "get_quote", "get_technical_indicator", "get_time_series"}),
    "twitter_x": frozenset({
        "get_followers", "get_following", "get_liking_users", "get_me", "get_mentions",
        "get_my_timeline", "get_tweet", "get_tweet_metrics", "get_user", "get_user_by_id",
        "get_user_timeline", "search_recent", "search_users",
    }),
    "wechat_official": frozenset({"get_follower_info", "get_publish_status", "list_followers"}),
    "wechat_personal": frozenset({"get_bot_status", "get_qr_code", "list_contacts", "list_groups"}),
    "whatsapp": frozenset({
        "get_business_profile", "get_phone_number", "list_message_templates", "list_phone_numbers",
    }),
    "woocommerce": frozenset({
        "get_customer", "get_order", "get_product", "list_customers", "list_orders", "list_products",
    }),
    "youtube": frozenset({
        "get_channel", "get_video", "list_captions", "list_comments", "list_my_videos", "search",
    }),
}
_MCP_MUTATION_ACTIONS = {
    "answer_callback_query",
    "append_block_children",
    "append_document_text",
    "calculate",
    "categorize",
    "compose_music",
    "generate_music",
    "generate_image",
    "generate_sound_effect",
    "generate_video",
    "hide_comment",
    "invite",
    "nango_proxy",
    "prepare_upload",
    "review",
    "run",
    "text_to_dialogue",
    "text_to_speech",
}
_READ_ONLY_TOOLS = {
    "read_context_archive",
    "resident_maintenance_status",
    "website_content",
    "analyze_audio",
    "browse_web",
    "check_youtube_publication_setup",
    "draft_skill",
    "extract_data",
    "find_team_members",
    "get_current_time",
    "get_entity_info",
    "get_goal_status",
    "get_stat_status",
    "get_task_details",
    "inspect_file_engine",
    "inspect_narration_recovery",
    "search_tools",
    # Only an unwrappable or rejected call_tool is classified as itself; it
    # executes nothing. A valid one is classified as the tool it calls.
    "call_tool",
    "web_search",
    "web_event_search",
    "web_fetch",
    "read_file",
    "list_files",
    "glob_files",
    "grep_files",
    "workspace_search",
    "workspace_list_knowledge",
    "sandbox_read_file",
    "sandbox_status",
    "list_skills",
    "get_skill_details",
    "list_workflows",
    "list_workflow_definitions",
    "get_workflow",
    "validate_workflow",
    "verify_file_collection",
    "list_workflow_runs",
    "get_workflow_run",
    "get_agent_initiative",
    "list_agent_initiatives",
    "list_agent_initiative_runs",
    "list_agent_files",
    "list_documents",
    "list_sandbox_files",
    "list_scheduled_jobs",
    "list_workspace_flows",
    "plan_video_segments",
    "probe_media",
    "search_free_music",
    "query_agent_capabilities",
    "query_entity_agents",
    "query_scheduled_jobs",
    "rag",
    "read_agent_file",
    "read_context_archive",
    "read_content_ledger",
    "read_finance_ledger",
    "read_recruiting_ledger",
    "read_relationship_ledger",
    "read_stickman_topic_ledger",
    "query_ledger",
    "visualize_workspace_ledgers",
    "render_response_surface",
    "read_youtube_public_metrics",
    "search_documents",
    "search_tasks",
    "take_screenshot",
    "transcribe_audio",
    "transcribe_video",
    "validate_subtitles",
    "verify_stickman_final_media",
    "wait_media_jobs",
    # Task agent loop only: parks the loop until a time; executes nothing.
    "wait_until",
    "weather_search",
    "ws_get_draft",
    "ws_lint_draft",
    "ws_read_source",
    "ws_search_blueprints",
    "ws_search_capabilities",
    "ws_search_entity_agents",
    "ws_suggest_blueprint",
}


def _fixed_action(
    action_key: str,
    risk_level: str,
    title: str,
    resource_kind: str,
    operation: str,
    capability_id: str | None = None,
) -> RuntimeApprovalAction:
    return RuntimeApprovalAction(
        "action",
        action_key,
        risk_level,
        title,
        resource_kind,
        operation,
        capability_id=capability_id,
    )


_FIXED_TOOL_ACTIONS: dict[str, RuntimeApprovalAction] = {
    "prepare_recording": _fixed_action("workspace.file.create", "medium", "prepare recording transcript", "file", "create"),
    "prepare_creative_studio": _fixed_action("workspace.file.create", "medium", "freeze creative production inputs", "file", "create"),
    "verify_creative_studio": _fixed_action("workspace.file.create", "medium", "verify creative files and render previews", "file", "create"),
    "compose_creative_publication": _fixed_action("workspace.file.create", "medium", "compose native illustrated publication", "file", "create"),
    "compose_creative_dub": _fixed_action("workspace.file.create", "medium", "compose translated video with actual narration", "file", "create"),
    "render_recording": _fixed_action("workspace.file.create", "medium", "render recording deliverables", "file", "create"),
    "align_subtitles": _fixed_action("workspace.file.create", "medium", "create subtitle files", "file", "create"),
    "answer_task_blocker": _fixed_action("workspace.task.update", "medium", "answer task blocker", "workspace_task", "modify"),
    "request_input": _fixed_action("human.input.request", "low", "ask the user a question", "conversation", "read", "human.input"),
    "build_narration_timeline": _fixed_action("workspace.file.create", "medium", "create narration timeline", "file", "create"),
    "code": _fixed_action("cli.exec", "medium", "run code", "cli", "execute", "cli.execute"),
    "compose_video_timeline": _fixed_action("workspace.file.create", "medium", "render video timeline", "file", "create"),
    "create_goal": _fixed_action("workspace.goal.create", "medium", "create goal", "goal", "create"),
    "dashboard_submit_module": _fixed_action("workspace.dashboard.module.update", "medium", "submit dashboard module", "workspace", "modify"),
    "generate_image": _fixed_action("workspace.file.create", "medium", "generate image", "file", "create"),
    "generate_video": _fixed_action("workspace.file.create", "medium", "generate video", "file", "create"),
    "import_free_music": _fixed_action("workspace.file.create", "medium", "import licensed music", "file", "create"),
    "interact_with_page": _fixed_action("browser.interact", "low", "interact with browser page", "browser", "execute", "manor.composite"),
    "merge_videos": _fixed_action("workspace.file.create", "medium", "merge videos", "file", "create"),
    "normalize_audio_loudness": _fixed_action("workspace.file.create", "medium", "normalize audio", "file", "create"),
    "notify_user": _fixed_action("external_message.send", "high", "notify user", "external_account", "send"),
    "prepare_narration_timeline": _fixed_action("workspace.file.create", "medium", "prepare narration timeline", "file", "create"),
    "provision_agent": _fixed_action("workspace.agent.create", "high", "provision agent", "agent", "create"),
    "publish_site": _fixed_action("workspace.site.publish", "high", "publish site", "site", "publish"),
    "record_content_ledger": _fixed_action("workspace.knowledge.update", "medium", "update content ledger", "knowledge", "modify"),
    "record_finance_ledger": _fixed_action("workspace.knowledge.update", "medium", "update finance ledger", "knowledge", "modify"),
    "record_recruiting_ledger": _fixed_action("workspace.knowledge.update", "medium", "update Recruiting/HR ledger", "knowledge", "modify"),
    "record_relationship_ledger": _fixed_action("workspace.knowledge.update", "medium", "update relationship ledger", "knowledge", "modify"),
    "record_stickman_topic_ledger": _fixed_action("workspace.knowledge.update", "medium", "update Stickman Topic Ledger", "knowledge", "modify"),
    "record_youtube_publication": _fixed_action("workspace.knowledge.update", "medium", "record YouTube publication", "knowledge", "modify"),
    "record_youtube_workspace_metrics": _fixed_action("workspace.knowledge.update", "medium", "record YouTube metrics", "knowledge", "modify"),
    "render_frame_samples": _fixed_action("workspace.file.create", "medium", "render frame samples", "file", "create"),
    "sandbox_cancel": _fixed_action("sandbox.cancel", "medium", "cancel sandbox command", "sandbox", "cancel"),
    "sandbox_respond": _fixed_action("sandbox.respond", "low", "respond to sandbox command", "sandbox", "execute"),
    "send_channel_attachment": _fixed_action(
        "channel.reply",
        "high",
        "send channel attachment",
        "external_account",
        "send",
        "external.message",
    ),
    "still_to_video": _fixed_action("workspace.file.create", "medium", "render video", "file", "create"),
    "update_goal_value": _fixed_action("workspace.goal.update", "medium", "update goal", "goal", "modify"),
    "update_task": _fixed_action("workspace.task.update", "medium", "update task", "workspace_task", "modify"),
    "video_edit": _fixed_action("workspace.file.modify", "medium", "edit video", "file", "modify"),
    "coding_based_video": _fixed_action(
        "workspace.file.create", "medium", "create coding-based video", "file", "create"
    ),
    "workspace_resolve_hitl": _fixed_action("workspace.hitl.resolve", "medium", "resolve Workspace request", "workspace", "modify"),
    "ws_assign_staff": _fixed_action("workspace.operation.draft", "medium", "assign Workspace staff", "workspace_operation", "modify"),
    "ws_attach_knowledge": _fixed_action("workspace.operation.draft", "medium", "attach Workspace knowledge", "workspace_operation", "modify"),
    "ws_commit_basics": _fixed_action("workspace.operation.draft", "medium", "update Workspace draft", "workspace_operation", "modify"),
    "ws_configure_app_view": _fixed_action("workspace.operation.draft", "medium", "configure Workspace app view", "workspace_operation", "modify"),
    "ws_flag_missing_integration": _fixed_action("workspace.operation.draft", "medium", "update Workspace integration requirement", "workspace_operation", "modify"),
    "ws_mark_ready": _fixed_action("workspace.operation.draft", "medium", "mark Workspace draft ready", "workspace_operation", "modify"),
    "ws_propose_agent_mapping": _fixed_action("workspace.operation.draft", "medium", "update Workspace agent mapping", "workspace_operation", "modify"),
    "ws_propose_automation": _fixed_action("workspace.operation.draft", "medium", "update Workspace automation", "workspace_operation", "modify"),
    "ws_propose_channel": _fixed_action("workspace.operation.draft", "medium", "update Workspace channel", "workspace_operation", "modify"),
    "ws_propose_goal": _fixed_action("workspace.operation.draft", "medium", "update Workspace goal", "workspace_operation", "modify"),
    "ws_propose_rule": _fixed_action("workspace.operation.draft", "medium", "update Workspace rule", "workspace_operation", "modify"),
    "ws_propose_service": _fixed_action("workspace.operation.draft", "medium", "update Workspace service", "workspace_operation", "modify"),
    "ws_remove": _fixed_action("workspace.operation.draft", "medium", "remove Workspace draft item", "workspace_operation", "modify"),
    "ws_request_custom_agent": _fixed_action("workspace.operation.draft", "medium", "request custom Workspace agent", "workspace_operation", "modify"),
    "ws_confirm_creation_preferences": _fixed_action("workspace.operation.draft", "medium", "confirm Workspace creation preferences", "workspace_operation", "modify"),
    "ws_set_autonomy": _fixed_action("workspace.operation.draft", "medium", "update Workspace autonomy", "workspace_operation", "modify"),
    "ws_set_budget": _fixed_action("workspace.operation.draft", "medium", "update Workspace budget", "workspace_operation", "modify"),
    "ws_set_evaluation": _fixed_action("workspace.operation.draft", "medium", "update Workspace evaluation", "workspace_operation", "modify"),
}
_FILE_CREATE_TOOLS = {"sandbox_save_result"}
_FILE_MUTATION_BASE_CMDS = {"mv", "chmod", "sed"}
_FILE_CREATE_BASE_CMDS = {"mkdir", "touch", "cp", "tee"}
_FILE_DELETE_BASE_CMDS = {"rm"}
# NOTE: a bare ">" used to live here. It produced false positives on any
# command that contained stderr/fd redirection like `cmd 2>&1` or `cmd 2>&3`,
# both of which only re-route file descriptors and do NOT write to a file.
# `_command_has_redirection` already detects real `>` / `>>` redirections to
# a file target (its negated character class rejects `>&`), so the bare hint
# is redundant. Keeping it caused `python pre_gate_file_check.py ... 2>&1`
# to be misclassified as workspace.file.modify and trigger an approval card
# on a read-only diagnostic script.
_CLI_WRITE_HINTS = ("tee ", "sed -i", " rm ", " mv ", " cp ", " mkdir ", " touch ", "chmod ")
_SHELL_SEGMENT_SEPARATOR_RE = re.compile(r"(?:^|\s)(?:&&|\|\||;|\|)(?:\s|$)")
_MANOR_ACTION_MAP = {
    "create_task": ("workspace.task.create", "medium", "create task", "workspace_task", "create"),
    "assign_task": ("workspace.task.update", "medium", "assign task", "workspace_task", "modify"),
    "start_workspace_draft": ("workspace.draft.start", "low", "start workspace setup", "workspace", "create"),
    "create_workspace": ("workspace.draft.start", "low", "start workspace setup", "workspace", "create"),
    "continue_workspace_draft": ("workspace.draft.update", "low", "update workspace setup", "workspace", "modify"),
    "create_client": ("workspace.client.create", "medium", "create client", "client", "create"),
    "delete_client": ("workspace.client.delete", "high", "delete client", "client", "delete"),
    "update_client": ("workspace.client.update", "medium", "update client", "client", "modify"),
    "prepare_client_delivery": ("workspace.client.delivery.prepare", "high", "prepare private Workspace invitation", "client", "create"),
    "rotate_client_delivery": ("workspace.client.delivery.rotate", "high", "rotate private Workspace invitation", "client", "modify"),
    "revoke_client_delivery": ("workspace.client.delivery.revoke", "high", "revoke private Workspace invitation", "client", "modify"),
    "link_client_relationship": ("workspace.client.relationship.link", "medium", "link CRM relationship", "client", "modify"),
    "unlink_client_relationship": ("workspace.client.relationship.unlink", "medium", "unlink CRM relationship", "client", "modify"),
    "create_client_contact": ("workspace.client.contact.create", "medium", "create client contact", "client", "create"),
    "update_client_contact": ("workspace.client.contact.update", "medium", "update client contact", "client", "modify"),
    "delete_client_contact": ("workspace.client.contact.delete", "high", "delete client contact", "client", "delete"),
    "create_client_activity": ("workspace.client.activity.create", "medium", "create client activity", "client", "create"),
    "update_client_activity": ("workspace.client.activity.update", "medium", "update client activity", "client", "modify"),
    "delete_client_activity": ("workspace.client.activity.delete", "high", "delete client activity", "client", "delete"),
    "create_client_opportunity": ("workspace.client.opportunity.create", "medium", "create client opportunity", "client", "create"),
    "update_client_opportunity": ("workspace.client.opportunity.update", "medium", "update client opportunity", "client", "modify"),
    "delete_client_opportunity": ("workspace.client.opportunity.delete", "high", "delete client opportunity", "client", "delete"),
    "create_client_followup": ("workspace.client.followup.create", "medium", "create client followup", "client", "create"),
    "update_client_followup": ("workspace.client.followup.update", "medium", "update client followup", "client", "modify"),
    "delete_client_followup": ("workspace.client.followup.delete", "high", "delete client followup", "client", "delete"),
    "create_order": ("workspace.order.create", "medium", "create order", "order", "create"),
    "create_skill": ("workspace.skill.create", "medium", "create skill", "skill", "create"),
    "delete_skill": ("workspace.skill.delete", "high", "delete skill", "skill", "delete"),
    "create_scheduled_job": ("workspace.automation.create", "high", "create automation", "automation", "create"),
    "cancel_scheduled_job": ("workspace.automation.delete", "high", "cancel automation", "automation", "delete"),
    "toggle_scheduled_job": ("workspace.automation.update", "medium", "toggle automation", "automation", "modify"),
    "run_scheduled_job_now": ("workspace.automation.run", "medium", "run automation", "automation", "execute"),
    "delete_document": ("workspace.file.delete", "high", "delete workspace document", "file", "delete"),
}
_SCHEDULED_JOB_ACTION_MAP = {
    "create_scheduled_job": ("workspace.automation.create", "high", "create automation", "automation", "create"),
    "cancel_scheduled_job": ("workspace.automation.delete", "high", "cancel automation", "automation", "delete"),
    "toggle_scheduled_job": ("workspace.automation.update", "medium", "toggle automation", "automation", "modify"),
    "run_scheduled_job_now": ("workspace.automation.run", "medium", "run automation", "automation", "execute"),
}
_AGENT_INITIATIVE_ACTION_MAP = {
    "create_agent_initiative": (
        "workspace.automation.agent_initiative.create",
        "high",
        "create Agent Initiative",
        "create",
    ),
    "update_agent_initiative": (
        "workspace.automation.agent_initiative.update",
        "medium",
        "update Agent Initiative",
        "modify",
    ),
    "mute_agent_initiative": (
        "workspace.automation.agent_initiative.mute",
        "medium",
        "mute Agent Initiative",
        "modify",
    ),
    "unmute_agent_initiative": (
        "workspace.automation.agent_initiative.unmute",
        "medium",
        "unmute Agent Initiative",
        "modify",
    ),
    "pause_agent_initiative": (
        "workspace.automation.agent_initiative.pause",
        "medium",
        "pause Agent Initiative",
        "modify",
    ),
    "resume_agent_initiative": (
        "workspace.automation.agent_initiative.resume",
        "medium",
        "resume Agent Initiative",
        "modify",
    ),
    "delete_agent_initiative": (
        "workspace.automation.agent_initiative.delete",
        "high",
        "delete Agent Initiative",
        "delete",
    ),
    "run_agent_initiative_now": (
        "workspace.automation.agent_initiative.run",
        "medium",
        "run Agent Initiative",
        "execute",
    ),
}
_WORKSPACE_AGENT_ACTION_MAP = {
    "create_task": ("workspace.task.create", "medium", "create workspace task", "workspace_task", "create"),
    "update_task_runtime": ("workspace.task.update", "medium", "update task runtime requirements", "workspace_task", "modify"),
    "create_knowledge_folder": ("workspace.knowledge.update", "medium", "create workspace Knowledge Net", "knowledge", "create"),
    "add_knowledge_documents": ("workspace.knowledge.update", "medium", "attach workspace knowledge documents", "knowledge", "modify"),
    "remove_knowledge_document": ("workspace.knowledge.update", "medium", "detach workspace knowledge document", "knowledge", "modify"),
    "update_knowledge_policy": ("workspace.knowledge.update", "medium", "update workspace knowledge policy", "knowledge", "modify"),
    "add_rule": ("workspace.rule.update", "medium", "update workspace rules", "workspace_rule", "modify"),
    "delegate_service": ("workspace.service.delegate", "medium", "delegate workspace service agent", "workspace_service", "execute"),
    "operation": ("workspace.operation.update", "medium", "update workspace operation draft", "workspace_operation", "modify"),
    "request_strategist_review": ("workspace.strategist.run", "medium", "request strategist review", "workspace", "execute"),
    "resolve_hitl": ("workspace.hitl.resolve", "medium", "resolve Workspace request", "workspace", "modify"),
    "answer_task_blocker": ("workspace.task.update", "medium", "answer task blocker", "workspace_task", "modify"),
    "update_goal_value": ("workspace.goal.update", "medium", "update goal", "goal", "modify"),
    "workspace_create_task": ("workspace.task.create", "medium", "create workspace task", "workspace_task", "create"),
    "workspace_update_task_runtime": ("workspace.task.update", "medium", "update task runtime requirements", "workspace_task", "modify"),
    "workspace_create_knowledge_folder": ("workspace.knowledge.update", "medium", "create workspace Knowledge Net", "knowledge", "create"),
    "workspace_add_knowledge_documents": ("workspace.knowledge.update", "medium", "attach workspace knowledge documents", "knowledge", "modify"),
    "workspace_remove_knowledge_document": ("workspace.knowledge.update", "medium", "detach workspace knowledge document", "knowledge", "modify"),
    "workspace_update_knowledge_policy": ("workspace.knowledge.update", "medium", "update workspace knowledge policy", "knowledge", "modify"),
    "workspace_operation": ("workspace.operation.update", "medium", "update workspace operation draft", "workspace_operation", "modify"),
    "workspace_add_rule": ("workspace.rule.update", "medium", "update workspace rules", "workspace_rule", "modify"),
    "workspace_request_strategist_review": ("workspace.strategist.run", "medium", "request strategist review", "workspace", "execute"),
}
_WORKSPACE_OPERATION_READ_ACTIONS = {
    "get_current",
    "validate_draft",
    "preview_diff",
}
_WORKSPACE_OPERATION_ACTION_MAP = {
    "create_draft": ("workspace.operation.draft", "medium", "create workspace operation draft", "workspace_operation", "create"),
    "patch_draft": ("workspace.operation.draft", "medium", "patch workspace operation draft", "workspace_operation", "modify"),
    "apply_draft": ("workspace.operation.apply", "high", "apply workspace operation draft", "workspace_operation", "modify"),
    "discard_draft": ("workspace.operation.discard", "medium", "discard workspace operation draft", "workspace_operation", "modify"),
}

def _classify_known_runtime_tool_action(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    entity_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    workspace_file_scope: RuntimeWorkspaceFileScope | None = None,
) -> RuntimeApprovalAction | None:
    """Map a concrete tool call to governance action-key language."""
    args = arguments or {}
    name = str(tool_name or "").strip()
    if not name or name in _READ_ONLY_TOOLS:
        return None

    if name.startswith("mcp__"):
        return _classify_mcp_tool_action(name, args)

    if name == "archive_file_collection":
        return RuntimeApprovalAction(
            "action", "workspace.file.create", "medium", "archive verified source media",
            "file", "create",
        )


    if workspace_file_scope is not None and workspace_file_scope.media_output_plan is not None:
        plan = workspace_file_scope.media_output_plan
        return _classify_file_write(
            path=plan.targets[0].rel_path.removeprefix(f"{plan.artifact_base_dir}/"), entity_id=entity_id,
            title=f"generate {plan.kind}", default_key="workspace.file.create",
            existence=WorkspaceFileExistence.MISSING,
        )

    if name in _AGENT_INITIATIVE_ACTION_MAP:
        key, risk, title, operation = _AGENT_INITIATIVE_ACTION_MAP[name]
        return RuntimeApprovalAction(
            "action",
            key,
            risk,
            title,
            "agent_initiative",
            operation,
            str(args.get("initiative_id") or "").strip() or None,
            "initiative.manage",
        )

    fixed_action = _FIXED_TOOL_ACTIONS.get(name)
    if fixed_action is not None:
        return fixed_action

    # Historical approval records still need classification. These retired
    # names have no registered handler and cannot execute new file writes.
    if name == "generate_document_file":
        from packages.core.ai.runtime.authorization_receipts import (
            WorkspaceFileAuthorizationResourceFactory,
        )

        document_name = str(args.get("name") or "")
        resources = WorkspaceFileAuthorizationResourceFactory.generate_document_resources(
            args,
            document_name,
            workspace_scoped=bool(str(workspace_id or "").strip()),
        )
        existence = (
            workspace_file_scope.existence
            if workspace_file_scope is not None
            else None
        )
        if existence is None and workspace_id and task_id:
            existence = _workspace_task_artifact_existence(
                entity_id,
                task_id,
                resources[0].resource_id if resources else None,
            )
        return _classify_file_write(
            path=document_name,
            entity_id=entity_id,
            title="generate document",
            default_key="workspace.file.create",
            existence=existence,
        )

    if name == "write_file":
        write_path = str(args.get("path") or "")
        existence = (
            workspace_file_scope.existence
            if workspace_file_scope is not None
            else _workspace_write_file_existence(
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                path=write_path,
            )
        )
        return _classify_file_write(
            path=write_path,
            entity_id=entity_id,
            title="write workspace file",
            existence=existence,
        )
    if name in {"edit_file", "patch_file", "replace_file"}:
        title = {
            "edit_file": "edit workspace file",
            "patch_file": "patch workspace file",
            "replace_file": "replace workspace file",
        }[name]
        path_argument = "target_path" if name == "replace_file" else "path"
        return RuntimeApprovalAction("action", "workspace.file.modify", "medium", title, "file", "modify", str(args.get(path_argument) or "") or None)
    if name == "delete_file":
        return RuntimeApprovalAction("action", "workspace.file.delete", "high", "delete workspace file", "file", "delete", str(args.get("path") or "") or None)
    if name == "code_project":
        action = str(args.get("action") or "").strip()
        if action in {"list", "get"}:
            return None
        project = str(args.get("project_id") or args.get("adopt_path") or args.get("name") or "") or None
        if action == "create":
            return RuntimeApprovalAction("action", "workspace.file.create", "medium", "create code project", "file", "create", project)
        if args.get("delete") and not args.get("files"):
            return RuntimeApprovalAction("action", "workspace.file.delete", "high", "delete code project files", "file", "delete", project)
        # Create vs modify must equal what the file guard computes for the same
        # targets, so the Workspace receipt authorizes the inner mutation.
        return _classify_file_write(
            path=project or "",
            entity_id=entity_id,
            title="write code project files",
            existence=(
                workspace_file_scope.existence
                if workspace_file_scope is not None
                else WorkspaceFileExistence.UNKNOWN
            ),
        )

    if name == "bash":
        return _classify_bash_tool(args, entity_id=entity_id, workspace_file_scope=workspace_file_scope)

    if name in _SCHEDULED_JOB_ACTION_MAP:
        key, risk, title, resource_kind, operation = _SCHEDULED_JOB_ACTION_MAP[name]
        return RuntimeApprovalAction(
            "action",
            key,
            risk,
            title,
            resource_kind,
            operation,
            str(args.get("job_id") or "") or None,
        )

    if name == "generate_file":
        kind = str(args.get("kind") or "").strip().lower().replace("-", "_")
        if kind == "search":
            return None
        raw_params = args.get("params") or {}
        params = raw_params if isinstance(raw_params, dict) else {}
        output_name = str(
            args.get("name")
            or params.get("name")
            or params.get("output_name")
            or params.get("filename")
            or ""
        )
        existence = (
            workspace_file_scope.existence
            if workspace_file_scope is not None
            else None
        )
        if existence is None and output_name and workspace_id and task_id:
            from packages.core.ai.runtime.authorization_receipts import (
                WorkspaceFileAuthorizationResourceFactory,
            )

            resources = WorkspaceFileAuthorizationResourceFactory.generate_file_resources(
                args,
                output_name,
            )
            existence = _workspace_task_artifact_existence(
                entity_id,
                task_id,
                resources[0].resource_id if resources else None,
            )
        return _classify_file_write(
            path=output_name,
            entity_id=entity_id,
            title=f"generate {kind or 'file'}",
            default_key="workspace.file.create",
            existence=existence,
        )

    if name in _FILE_CREATE_TOOLS:
        return RuntimeApprovalAction("action", "workspace.file.create", "medium", "create workspace file", "file", "create")

    if name == "sandbox_write_file":
        return RuntimeApprovalAction("action", "sandbox.file.modify", "medium", "write sandbox file", "sandbox_file", "modify", str(args.get("path") or "") or None)
    if name == "sandbox_exec":
        command = str(args.get("command") or "")
        if _command_has_write_hint(command):
            return RuntimeApprovalAction("action", "sandbox.exec.write_unknown", "medium", "run sandbox command with possible writes", "sandbox", "execute")
        return RuntimeApprovalAction("action", "sandbox.exec", "low", "run sandbox command", "sandbox", "execute")
    if name == "sandbox_create":
        return RuntimeApprovalAction("action", "sandbox.create", "low", "create sandbox", "sandbox", "create")
    if name == "manor":
        from packages.core.ai.runtime.manor_read_policy import (
            ManorReadCapabilityFactory,
        )

        action_name = str(args.get("action") or "").strip()
        if ManorReadCapabilityFactory.supports(action_name):
            return None
        if not action_name:
            return RuntimeApprovalAction("action", "workspace.agent.action", "medium", "run Manor action", "workspace", "modify")
        mapped = _MANOR_ACTION_MAP.get(action_name)
        if mapped:
            key, risk, title, resource_kind, operation = mapped
            return RuntimeApprovalAction("action", key, risk, title, resource_kind, operation)
        if action_name.startswith("delete_"):
            return RuntimeApprovalAction("action", f"workspace.{action_name}", "high", "run destructive Manor action", "workspace", "delete")
        if action_name.startswith(("create_", "update_", "assign_", "sync_")):
            return RuntimeApprovalAction("action", f"workspace.{action_name}", "medium", "run Manor mutation", "workspace", "modify")
        return RuntimeApprovalAction("action", f"workspace.{action_name}", "medium", "run Manor action", "workspace", "modify")

    if name == "workspace_agent":
        raw_params = args.get("params") or {}
        params = raw_params if isinstance(raw_params, dict) else {}
        action_name = str(args.get("action") or "").strip()
        if action_name in {"search", "list_knowledge", "read_delegation_result"}:
            return None
        if action_name == "operation":
            return _classify_workspace_operation_action(params)
        mapped = _WORKSPACE_AGENT_ACTION_MAP.get(action_name)
        if mapped:
            key, risk, title, resource_kind, operation = mapped
            resource_id = str(
                params.get("task_id")
                or params.get("rule_key")
                or params.get("group_id")
                or params.get("document_id")
                or ""
            ) or None
            return RuntimeApprovalAction("action", key, risk, title, resource_kind, operation, resource_id)
        return RuntimeApprovalAction("action", "workspace.agent.action", "medium", "run Workspace Agent action", "workspace", "modify")

    if name in _WORKSPACE_AGENT_ACTION_MAP:
        if name == "workspace_list_knowledge":
            return None
        if name == "workspace_operation":
            return _classify_workspace_operation_action(args)
        key, risk, title, resource_kind, operation = _WORKSPACE_AGENT_ACTION_MAP[name]
        resource_id = str(
            args.get("task_id")
            or args.get("rule_key")
            or args.get("group_id")
            or args.get("document_id")
            or ""
        ) or None
        return RuntimeApprovalAction("action", key, risk, title, resource_kind, operation, resource_id)

    if name in {"create_skill", "update_skill", "delete_skill"}:
        operation = "delete" if name.startswith("delete_") else "create" if name.startswith("create_") else "modify"
        risk = "high" if operation == "delete" else "medium"
        return RuntimeApprovalAction("action", f"workspace.skill.{operation}", risk, f"{operation} skill", "skill", operation)

    workflow_actions = {
        "create_workflow": ("workspace.workflow.create", "medium", "create workflow", "create"),
        "import_workflow": ("workspace.workflow.create", "medium", "import workflow", "create"),
        "ai_edit_workflow": ("workspace.workflow.modify", "medium", "AI edit workflow", "modify"),
        "update_workflow": ("workspace.workflow.modify", "medium", "update workflow", "modify"),
        "deploy_workflow": ("workspace.workflow.deploy", "high", "deploy workflow", "publish"),
        "delete_workflow": ("workspace.workflow.delete", "high", "delete workflow", "delete"),
        # The launcher creates a paused Starter review card. Execution begins
        # only after the user submits that card, so a generic high-risk
        # approval here would duplicate the Flow's own explicit review.
        "start_workspace_flow": ("workspace.workflow.run", "medium", "prepare Workspace Flow", "create"),
        "run_workflow": ("workspace.workflow.run", "high", "run published workflow", "execute"),
        "test_workflow": ("workspace.workflow.run", "high", "test workflow", "execute"),
        "test_workflow_node": ("workspace.workflow.run", "high", "test workflow node", "execute"),
        "cancel_workflow_run": ("workspace.workflow.run", "medium", "cancel workflow run", "modify"),
        "resume_workflow_run": ("workspace.workflow.run", "high", "resume workflow run", "execute"),
    }
    if name in workflow_actions:
        key, risk, title, operation = workflow_actions[name]
        resource_id = str(args.get("workflow") or args.get("run_id") or "").strip() or None
        return RuntimeApprovalAction(
            "action",
            key,
            risk,
            title,
            "workflow",
            operation,
            resource_id,
            (
                "workflow.run"
                if name in {
                    "start_workspace_flow",
                    "cancel_workflow_run",
                    "resume_workflow_run",
                }
                else None
            ),
        )

    if name == "write_agent_file":
        return RuntimeApprovalAction("action", "workspace.agent_file.modify", "medium", "write agent file", "agent_file", "modify")

    if name.startswith(_DESTRUCTIVE_PREFIXES):
        return RuntimeApprovalAction("action", f"tool.{name}", "high", "run destructive tool", "tool", "delete")
    if name in _EXTERNAL_COMMIT_ACTIONS or name.startswith(_EXTERNAL_COMMIT_PREFIXES):
        return RuntimeApprovalAction(
            "action",
            "external_message.send",
            "high",
            "send external message",
            "external_account",
            "send",
            capability_id="external.message",
        )
    if name.startswith(_MUTATION_PREFIXES):
        operation = "create" if name.startswith("create_") else "modify"
        return RuntimeApprovalAction("action", f"tool.{name}", "medium", "run mutating tool", "tool", operation)
    return None


_ORCHESTRATOR_REASONS = {
    "invoke_skill": (
        "Every nested skill tool call re-enters the runtime authorization boundary."
    ),
    "execute_planned_action": (
        "Execute only the immutable action bound to the current Dispatcher lease. "
        "Dispatcher owns its approval; the handler revalidates that lease before I/O."
    ),
}
_CONTROL_REASONS = {
    "resident_maintenance_draft": (
        "Save a private draft for the verified resident in this conversation. "
        "The handler validates resident scope; only a separate browser confirmation writes to PMS."
    ),
    "submit_result": "Return a result to the current Runtime-owned orchestration loop.",
    "update_task_plan": (
        "Record the Task agent loop's own plan and acceptance criteria. "
        "It is bookkeeping for this loop and executes nothing."
    ),
    "complete_task": (
        "Submit the Task agent loop's final outcome to its host, which decides "
        "and writes the Task's terminal state."
    ),
    "sandbox_destroy": (
        "Release an ephemeral Runtime-owned sandbox, not a user file or resource. "
        "The handler validates the current conversation and actor before cleanup."
    ),
    "update_goal_value": (
        "Record a Goal measurement for the current Workspace run. "
        "The handler validates entity and Workspace scope; this is not an external action."
    ),
}

# Control tools a Runtime host arms for one concrete loop as dynamic
# handlers. They have no durable catalog binding: the loop that armed them is
# their only binding, and without a handler the call fails as unknown.
RUNTIME_LOOP_CONTROL_TOOL_NAMES = frozenset({
    "submit_result",
    "update_task_plan",
    "complete_task",
})

_NON_ACTION_CAPABILITY_BY_TOOL = {
    "invoke_skill": "skill.invoke",
    "read_file": "file.read",
    "list_files": "file.read",
    "glob_files": "file.read",
    "grep_files": "file.read",
    "workspace_search": "workspace.search",
    "workspace_list_knowledge": "workspace.search",
    "rag": "workspace.search",
    "search_tools": "runtime.discovery",
    "call_tool": "runtime.discovery",
    "list_skills": "runtime.discovery",
    "get_skill_details": "runtime.discovery",
    "read_context_archive": "runtime.discovery",
    "web_search": "web.safe_search",
    "web_fetch": "web.safe_search",
    "browse_web": "web.safe_search",
    "sandbox_read_file": "sandbox.execute",
    "sandbox_destroy": "sandbox.execute",
}

def runtime_policy_action_for_classification(
    classification: RuntimeToolClassification,
) -> tuple[RuntimeApprovalAction, str | None]:
    """The action and impact the policy gate evaluates for one classification.

    Shared by the execution gate and discovery so both judge a tool by the same
    facts. A classification without an approval action (reads, controls) is
    evaluated as its low-risk authorization action.
    """

    action = classification.action
    if action is not None:
        # A classified action that only reads (asking the user a question)
        # is a read for the policy. Without this its impact was unknown, and
        # unknown impact always asks: every question needed an approval.
        return action, ("read" if action.operation == "read" else None)
    authorization = classification.authorization
    impact = "read" if classification.effect is RuntimeToolEffect.READ_ONLY else "reversible"
    return RuntimeApprovalAction(
        kind="action", action_key=authorization.action_key,
        capability_id=authorization.capability_id,
        risk_level="low", title=authorization.action_key,
        resource_kind=authorization.resource_kind,
        resource_id=authorization.resource_id,
        operation="read" if impact == "read" else "modify",
    ), impact


def runtime_is_orchestration_tool(tool_name: str) -> bool:
    """True for tools that route, discover, delegate or control a run, not do its work.

    Derived from the classifier: orchestrator/control effects, the
    ``runtime.discovery`` capability, loop-armed control tools, and the
    human-input / delegation hand-offs. A Skill that declares only these has no
    work tools of its own.
    """

    from packages.core.ai.runtime.human_input import REQUEST_INPUT_TOOL_NAME
    from packages.core.ai.runtime.workspace_delegation_actions import DELEGATE_TO_AGENT_TOOL_NAME
    from packages.core.constants.execution import DELEGATE_TO_SERVICE_TOOL_NAME

    name = str(tool_name or "").strip()
    if not name:
        return False
    if (
        name in RUNTIME_LOOP_CONTROL_TOOL_NAMES
        or _NON_ACTION_CAPABILITY_BY_TOOL.get(name) == "runtime.discovery"
        or name in {REQUEST_INPUT_TOOL_NAME, DELEGATE_TO_AGENT_TOOL_NAME, DELEGATE_TO_SERVICE_TOOL_NAME}
    ):
        return True
    try:
        effect = classify_runtime_tool(name, {}).effect
    except Exception:
        return False
    return effect in {RuntimeToolEffect.ORCHESTRATOR, RuntimeToolEffect.CONTROL}


def _resolve_non_action_authorization(
    request: RuntimeToolClassificationRequest,
    effect: RuntimeToolEffect,
) -> RuntimeToolAuthorization:
    access_by_effect = {
        RuntimeToolEffect.READ_ONLY: RuntimeAuthorizationAccess.READ,
        RuntimeToolEffect.ORCHESTRATOR: RuntimeAuthorizationAccess.USE,
        RuntimeToolEffect.CONTROL: RuntimeAuthorizationAccess.CONTROL,
    }
    access = access_by_effect.get(effect)
    if access is None:
        raise ValueError(f"No non-action authorization mapping for {effect.value}")

    authorization_tool_name = request.tool_name
    if (
        request.tool_name == "workspace_agent"
        and str(request.arguments.get("action") or "").strip() == "operation"
    ):
        # The gateway and direct operation leaf need the same hard read gate.
        # Runtime still validates the original tool's current binding.
        authorization_tool_name = "workspace_operation"
    capability_id = _NON_ACTION_CAPABILITY_BY_TOOL.get(authorization_tool_name)
    if request.tool_name == "manor":
        from packages.core.ai.runtime.manor_read_policy import (
            ManorReadCapabilityFactory,
        )

        action_name = str(request.arguments.get("action") or "").strip()
        capability_id = ManorReadCapabilityFactory.create(action_name).value
    if capability_id is None and request.tool_name.startswith("mcp__"):
        capability_id = _MCP_AUTHORIZATION_CAPABILITY
    if capability_id is None:
        from packages.core.ai.runtime.capabilities import capabilities_for_tool_names

        matches = capabilities_for_tool_names({authorization_tool_name})
        if matches:
            capability_id = sorted(capability.id for capability in matches)[0]

    if effect is RuntimeToolEffect.ORCHESTRATOR and request.tool_name == "invoke_skill":
        action_key = "skill.invoke"
    elif effect is RuntimeToolEffect.CONTROL:
        action_key = f"runtime.control.{request.tool_name}"
    else:
        action_key = f"tool.{authorization_tool_name}.read"
    resource_kind = None
    resource_id = None
    if request.tool_name.startswith("mcp__chrome__"):
        chrome_name, _ = normalize_chrome_call(request.tool_name, request.arguments)
        action_key = f"tool.{chrome_name}.read"
        resource_kind = "browser"
        resource_id = "chrome"
    if request.tool_name == "search_tools" and _is_verified_chrome_tool_discovery(
        request.arguments
    ):
        resource_kind = "browser"
        resource_id = "chrome"
    return RuntimeToolAuthorization(
        action_key=action_key,
        capability_id=capability_id,
        access=access,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )


def _is_verified_chrome_tool_discovery(arguments: dict[str, Any]) -> bool:
    """Identify only canonical discovery requests that can load Chrome tools."""
    from packages.core.ai.runtime.tool_discovery import runtime_prepare_search_tools_request

    request = runtime_prepare_search_tools_request(
        mode=arguments.get("mode"),
        query=arguments.get("query"),
        queries=arguments.get("queries"),
        provider_key=arguments.get("provider_key"),
        exact_names=arguments.get("exact_names"),
        max_results=arguments.get("max_results"),
    )
    if not request.ok:
        return False
    if request.mode == "provider":
        return request.provider_key == "chrome"
    return (
        request.mode == "exact"
        and bool(request.exact_names)
        and all(name in CHROME_MCP_TOOLS for name in request.exact_names)
    )


@lru_cache(maxsize=1)
def _runtime_tool_classifier() -> RuntimeToolClassifier:
    return RuntimeToolClassifierFactory.create_default(
        read_only_predicate=_runtime_tool_is_explicitly_read_only,
        action_resolver=_classify_known_runtime_tool_action,
        authorization_resolver=_resolve_non_action_authorization,
        orchestrator_reasons=_ORCHESTRATOR_REASONS,
        control_reasons=_CONTROL_REASONS,
    )


def _canonical_composite_tool_call(
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Project composite gateway calls onto their governed legacy action."""

    from packages.core.ai.runtime.composite_tools import (
        RuntimeCompositeToolCallFactory,
    )

    call = RuntimeCompositeToolCallFactory.create(tool_name, arguments)
    return call.tool_name, call.arguments


def _is_update_goal_value_call(
    canonical_name: str,
    canonical_arguments: dict[str, Any],
) -> bool:
    if canonical_name in {"update_goal_value", "record_stat_assessment"}:
        return True
    if canonical_name in {"workspace_agent", "manor"}:
        return str(canonical_arguments.get("action") or "").strip() in {"update_goal_value", "record_stat_assessment"}
    return False


def classify_runtime_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    entity_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    workspace_file_scope: RuntimeWorkspaceFileScope | None = None,
    declared_effect: str | None = None,
) -> RuntimeToolClassification:
    """Classify a concrete call without conflating read-only and unknown.

    Every execution caller must branch on the returned enum.  The compatibility
    ``classify_runtime_tool_action`` helper remains available to code that only
    needs normalized governance metadata, but it must not be used as an
    execution allow/deny decision because read-only, orchestrator, and unknown
    calls all have no action object there.
    """

    canonical_name, canonical_arguments = _canonical_composite_tool_call(
        tool_name,
        arguments,
    )
    request = RuntimeToolClassificationRequest.create(
        canonical_name,
        canonical_arguments,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        workspace_file_scope=workspace_file_scope,
    )
    if _is_update_goal_value_call(canonical_name, canonical_arguments):
        return RuntimeToolClassification.action_call(_FIXED_TOOL_ACTIONS["update_goal_value"])
    if declared_effect is not None:
        try:
            resolved_effect = MCPActionEffect(
                str(declared_effect).strip().lower()
            )
        except ValueError:
            # An unrecognized vendor effect is executable external state, not
            # evidence that the operation is safe to read without approval.
            resolved_effect = MCPActionEffect.WRITE
        if resolved_effect is MCPActionEffect.READ:
            return RuntimeToolClassification.read_only(
                _resolve_non_action_authorization(
                    request,
                    RuntimeToolEffect.READ_ONLY,
                )
            )
        return RuntimeToolClassification.action_call(
            RuntimeMCPApprovalActionFactory.create(
                tool_name,
                resolved_effect,
            )
        )
    return _runtime_tool_classifier().classify(request)


class RuntimeMCPApprovalActionFactory:
    """Translate the shared MCP effect enum into governance metadata."""

    @staticmethod
    def create(
        tool_name: str,
        effect: MCPActionEffect,
    ) -> RuntimeApprovalAction:
        server, action = split_mcp_tool(tool_name)
        subject = ".".join(part for part in (server, action) if part)
        policy = OfficialRemoteMCPActionPolicyFactory.create(server, effect)
        return RuntimeApprovalAction(
            "action",
            subject or "mcp.external_action",
            policy.risk_level,
            policy.title,
            "external_account",
            policy.operation,
            capability_id=_MCP_AUTHORIZATION_CAPABILITY,
        )


def classify_runtime_tool_action(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    entity_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    workspace_file_scope: RuntimeWorkspaceFileScope | None = None,
) -> RuntimeApprovalAction | None:
    """Compatibility projection for policy metadata, not an allow decision."""

    return classify_runtime_tool(
        tool_name,
        arguments,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        workspace_file_scope=workspace_file_scope,
    ).action


def _runtime_tool_is_explicitly_read_only(
    tool_name: str,
    args: dict[str, Any],
) -> bool:
    if tool_name.startswith("mcp__chrome__"):
        return classify_chrome_call(tool_name, args).kind == "read"
    if tool_name in _READ_ONLY_TOOLS:
        return True
    if tool_name == "generate_file":
        return str(args.get("kind") or "").strip().lower().replace("-", "_") == "search"
    if tool_name == "code_project":
        return str(args.get("action") or "").strip() in {"list", "get"}
    if tool_name == "manor":
        from packages.core.ai.runtime.manor_read_policy import (
            ManorReadCapabilityFactory,
        )

        action = str(args.get("action") or "").strip()
        return ManorReadCapabilityFactory.supports(action)
    if tool_name == "workspace_agent":
        action = str(args.get("action") or "").strip()
        if action == "operation":
            params = args.get("params")
            return _runtime_tool_is_explicitly_read_only(
                "workspace_operation", params if isinstance(params, dict) else {},
            )
        return action in {
            "search",
            "list_knowledge",
            "get_goal_status",
            "get_stat_status",
            "visualize_ledgers",
            "read_delegation_result",
        }
    if tool_name == "workspace_operation":
        from packages.core.ai.runtime.workspace_operation_actions import (
            _resolve_workspace_operation_action,
        )

        action = _resolve_workspace_operation_action(args)
        return bool(action and action in _WORKSPACE_OPERATION_READ_ACTIONS)
    if not tool_name.startswith("mcp__"):
        return False
    server, action = split_mcp_tool(tool_name)
    if not server or not action:
        return False
    if server == "manor_pms":
        return _manor_pms_action_effect(action) is MCPActionEffect.READ
    return action in _MCP_READ_ONLY_ACTIONS_BY_SERVER.get(server, frozenset())


@lru_cache(maxsize=1)
def _manor_pms_action_effects() -> dict[str, MCPActionEffect]:
    """Use the shipped PMS contract for both discovery and approval policy."""
    from packages.core.ai.mcp import manor_pms

    return {
        tool["name"]: (
            MCPActionEffect.DESTRUCTIVE
            if tool.get("annotations", {}).get("destructiveHint") is True
            else MCPActionEffect.READ
            if tool.get("annotations", {}).get("readOnlyHint") is True
            else MCPActionEffect.WRITE
        )
        for tool in manor_pms.list_tools()
    }


def _manor_pms_action_effect(action: str) -> MCPActionEffect:
    return _manor_pms_action_effects().get(action, MCPActionEffect.WRITE)


def _classify_workspace_operation_action(args: dict[str, Any]) -> RuntimeApprovalAction | None:
    from packages.core.ai.runtime.workspace_operation_actions import _resolve_workspace_operation_action

    action_name = _resolve_workspace_operation_action(args)
    if action_name in _WORKSPACE_OPERATION_READ_ACTIONS:
        return None
    mapped = _WORKSPACE_OPERATION_ACTION_MAP.get(action_name)
    if mapped:
        key, risk, title, resource_kind, operation = mapped
        resource_id = str(args.get("draft_id") or "") or None
        return RuntimeApprovalAction("action", key, risk, title, resource_kind, operation, resource_id)
    # Unknown/missing leaf actions have no executable effect contract. They
    # must remain UNKNOWN rather than borrowing a generic mutation's approval.
    return None


def _classify_mcp_tool_action(tool_name: str, args: dict[str, Any]) -> RuntimeApprovalAction | None:
    server, action = split_mcp_tool(tool_name)
    if not server or not action:
        return None

    if server == "apify" and action in {
        "start_instagram_collection", "cancel_instagram_collection",
    }:
        return RuntimeMCPApprovalActionFactory.create(tool_name, MCPActionEffect.WRITE)

    if server == "manor_pms":
        effect = _manor_pms_action_effect(action)
        if effect is MCPActionEffect.READ:
            return None
        return RuntimeMCPApprovalActionFactory.create(tool_name, effect)

    if server == "chrome":
        # run_sequence is the host composite; its children cross this gate too.
        canonical_name, _ = normalize_chrome_call(tool_name, args)
        if tool_name not in CHROME_MCP_TOOLS and canonical_name not in CHROME_MCP_TOOLS and action != "run_sequence":
            return None
        if action != "confirm_action":
            intent = args.get("operation_intent")
            if isinstance(intent, dict) and intent.get("effect") == "publish":
                return RuntimeApprovalAction(
                    "action", "chrome.publication.publish", "high", "publish Chrome content",
                    "external_account", "publish", capability_id="manor.composite",
                )
            if action == "run_sequence":
                return RuntimeApprovalAction(
                    "action", "chrome.browser.interact", "low", "run Chrome sequence",
                    "browser", "modify", capability_id="manor.composite",
                )
            # File selection keeps its existing provider-side confirmation
            # contract.  It is not an ordinary browser action, but its risk
            # level must not be upgraded solely by this basic-action policy.
            if action in {"upload", "set_files", "upload_ref"}:
                return RuntimeApprovalAction(
                    "action", "chrome.browser.interact", "low", "interact with Chrome",
                    "browser", "execute", capability_id="manor.composite",
                )
            decision = classify_chrome_call(tool_name, args)
            if decision.kind in {"read", "ordinary"}:
                return RuntimeApprovalAction(
                    "action", "chrome.browser.interact", "low", "interact with Chrome",
                    "browser", "read" if decision.kind == "read" else "modify",
                    capability_id="manor.composite",
                )
            if decision.kind in {"unsupported", "observation_required"}:
                return None
            confirmation = decision.confirmation
            if confirmation is None:
                return None
            return RuntimeApprovalAction(
                "action", f"chrome.{confirmation.policy_category}.confirm", "high",
                "confirm Chrome action", "external_account",
                "execute",
                capability_id="manor.composite",
            )
        mode = str(args.get("confirmation_mode") or "").strip()
        category = re.sub(r"[^a-z0-9_]+", "_", str(args.get("policy_category") or "browser_action").strip().lower()).strip("_") or "browser_action"
        if mode == "preapproval_allowed" and args.get("preapproved") is True:
            return RuntimeApprovalAction(
                "action",
                f"chrome.{category}.execute",
                "low",
                "execute preapproved Chrome action",
                "external_account",
                "execute",
                capability_id="manor.composite",
            )
        return RuntimeApprovalAction(
            "action",
            f"chrome.{category}.confirm",
            "high",
            "confirm Chrome action",
            "external_account",
            "execute",
            str(args.get("approvalId") or args.get("approval_id") or "").strip() or None,
            "manor.composite",
        )

    if (
        server == "youtube"
        and action == "upload_video"
        and str(args.get("privacy") or "private").strip().lower() == "public"
    ):
        return RuntimeApprovalAction(
            "action",
            "social_post.publish",
            "high",
            "publish public YouTube video",
            "external_account",
            "publish",
            capability_id="external.social",
        )

    if server == "facebook" and action == "create_live_video":
        status = str(args.get("status") or "LIVE_NOW").strip().upper()
        if status == "LIVE_NOW":
            return RuntimeApprovalAction(
                "action",
                "social_post.publish",
                "high",
                "start public Facebook live video",
                "external_account",
                "publish",
                capability_id="external.social",
            )
        return RuntimeApprovalAction(
            "action",
            "facebook.create_live_video",
            "medium",
            "prepare Facebook live video",
            "external_account",
            "create",
            capability_id="external.social",
        )
    if server == "facebook" and action == "end_live_video":
        return RuntimeApprovalAction(
            "action",
            "facebook.end_live_video",
            "high",
            "end Facebook live video",
            "external_account",
            "modify",
            capability_id="external.social",
        )

    if server in {"twitter_x", "linkedin", "facebook"}:
        if action in _SOCIAL_PUBLISH_ACTIONS:
            return RuntimeApprovalAction("action", "social_post.publish", "high", "publish social post", "external_account", "publish")
        if action in _SOCIAL_MUTATION_ACTIONS:
            risk = "high" if action.startswith("delete_") else "medium"
            suffix = "delete" if action.startswith("delete_") else "mutate"
            operation = "delete" if suffix == "delete" else "modify"
            return RuntimeApprovalAction("action", f"social_post.{suffix}", risk, f"{suffix} social content", "external_account", operation)

    if server in {"gmail", "outlook", "email"}:
        if server == "email" and action == "save_attachment_to_workspace":
            return RuntimeApprovalAction(
                "action",
                "workspace.knowledge.update",
                "medium",
                "save email attachment to Workspace Knowledge",
                "knowledge",
                "create",
            )
        if action in _EMAIL_SEND_ACTIONS:
            return RuntimeApprovalAction("action", "email.send", "high", "send email", "external_account", "send")
        if action.startswith("delete_"):
            return RuntimeApprovalAction("action", "email.delete", "medium", "delete email item", "external_account", "delete")
        if action in {"create_draft", "update_draft"}:
            return RuntimeApprovalAction("action", "email.draft", "low", "modify email draft", "external_account", "modify")

    if server in {"wechat_official", "facebook"} and action in _MESSAGE_SEND_ACTIONS:
        return RuntimeApprovalAction("action", "external_message.send", "high", "send external message", "external_account", "send")

    if server == "twilio" and action == "make_call":
        return RuntimeApprovalAction(
            "action",
            "external_message.send",
            "high",
            "place external call",
            "external_account",
            "send",
            capability_id="external.message",
        )

    official_remote = _OFFICIAL_REMOTE_MCP_DEFINITIONS.get(server)
    if official_remote is not None:
        if action in official_remote.read_only_actions:
            return None
        # Vendor tools/list is authoritative and can add actions between
        # Manor releases. Unknown payment actions fail closed as writes until
        # their effect is added to the shared provider enum.
        return RuntimeApprovalAction(
            "action",
            f"{server}.{action}",
            "high",
            "run external payment action",
            "external_account",
            "modify",
            capability_id=_MCP_AUTHORIZATION_CAPABILITY,
        )


    if action in _NON_REPRESENTATIONAL_EXTERNAL_ACTIONS:
        return RuntimeApprovalAction(
            "action",
            "external_presence.update",
            "low",
            "update external presence",
            "external_account",
            "modify",
            capability_id="external.social",
        )

    if action.startswith(_DESTRUCTIVE_PREFIXES):
        return RuntimeApprovalAction(
            "action",
            f"{server}.{action}",
            "high",
            "run destructive external action",
            "external_account",
            "delete",
            capability_id=_MCP_AUTHORIZATION_CAPABILITY,
        )
    if action in _EXTERNAL_COMMIT_ACTIONS or action.startswith(_EXTERNAL_COMMIT_PREFIXES):
        if any(marker in server for marker in (
            "twitter", "linkedin", "facebook", "instagram", "tiktok",
            "youtube", "xiaohongshu", "rednote",
        )):
            return RuntimeApprovalAction(
                "action",
                "social_post.publish",
                "high",
                "publish social content",
                "external_account",
                "publish",
                capability_id="external.social",
            )
        if any(marker in server for marker in (
            "gmail", "outlook", "email", "mail", "smtp", "imap",
        )):
            return RuntimeApprovalAction(
                "action",
                "email.send",
                "high",
                "send email",
                "external_account",
                "send",
                capability_id="external.email",
            )
        return RuntimeApprovalAction(
            "action",
            "external_message.send",
            "high",
            "send external message",
            "external_account",
            "send",
            capability_id="external.message",
        )
    if action in _MCP_MUTATION_ACTIONS or action.startswith(_MUTATION_PREFIXES):
        risk = "medium"
        if _looks_public_or_paid(server, action, args):
            risk = "high"
        return RuntimeApprovalAction(
            "action",
            f"{server}.{action}",
            risk,
            "run external action",
            "external_account",
            "modify",
            capability_id=_MCP_AUTHORIZATION_CAPABILITY,
        )
    return None


def _workspace_task_artifact_existence(
    entity_id: str | None,
    task_id: str | None,
    resource_id: str | None,
) -> WorkspaceFileExistence:
    """Resolve an exact task artifact without requiring an async DB lookup."""

    normalized_task_id = str(task_id or "").strip()
    normalized_resource = str(resource_id or "").strip().replace("\\", "/").strip("/")
    if (
        not normalized_task_id
        or "/" in normalized_task_id
        or not normalized_resource
        or any(part == ".." for part in normalized_resource.split("/"))
    ):
        return WorkspaceFileExistence.UNKNOWN
    entity_root = _entity_abs_path(entity_id, ".")
    if entity_root is None:
        return WorkspaceFileExistence.UNKNOWN
    storage_root = os.path.join(entity_root, "Workspaces", "_by_id")
    if not os.path.isdir(storage_root):
        return WorkspaceFileExistence.MISSING
    try:
        folders = tuple(os.scandir(storage_root))
    except OSError:
        return WorkspaceFileExistence.UNKNOWN
    for folder in folders:
        if not folder.is_dir(follow_symlinks=False):
            continue
        candidate = os.path.realpath(os.path.join(
            folder.path,
            "tasks",
            normalized_task_id,
            normalized_resource,
        ))
        task_root = os.path.realpath(os.path.join(
            folder.path,
            "tasks",
            normalized_task_id,
        ))
        if candidate != task_root and not candidate.startswith(task_root + os.sep):
            return WorkspaceFileExistence.UNKNOWN
        if os.path.exists(candidate):
            return WorkspaceFileExistence.EXISTS
    return WorkspaceFileExistence.MISSING


def code_project_target_paths(root_path: str, arguments: dict[str, Any]) -> list[str]:
    """Entity-relative targets of one code_project write, exactly as the handler guards them."""
    from packages.core.ai.runtime.code_projects import clean_rel

    if arguments.get("files"):
        raw = [item.get("path") for item in arguments["files"] if isinstance(item, dict)]
    else:
        raw = list(arguments.get("delete") or [])
    targets = []
    for value in raw:
        rel = clean_rel(value)
        if rel:
            targets.append(f"{root_path}/{rel}")
    return targets


async def _code_project_workspace_file_scope(
    entity_id: str,
    arguments: dict[str, Any],
) -> RuntimeWorkspaceFileScope | None:
    """Bind a code_project write to its project folder and exact target files."""
    if str(arguments.get("action") or "").strip() != "write":
        return None
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.code_project import CodeProject

    project_id = str(arguments.get("project_id") or "").strip()
    async with async_session() as db:
        if not hasattr(db, "execute"):
            return None
        root_path = (await db.execute(
            select(CodeProject.root_path).where(
                CodeProject.id == project_id,
                CodeProject.entity_id == entity_id,
                CodeProject.archived_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
    if not root_path:
        return RuntimeWorkspaceFileScope(WorkspaceFileExistence.UNKNOWN)
    if not arguments.get("files"):
        return RuntimeWorkspaceFileScope(WorkspaceFileExistence.EXISTS, root_path)
    exists = [
        _entity_file_exists(entity_id, target)
        for target in code_project_target_paths(root_path, arguments)
    ]
    if any(item is True for item in exists):
        existence = WorkspaceFileExistence.EXISTS
    elif exists and all(item is False for item in exists):
        existence = WorkspaceFileExistence.MISSING
    else:
        existence = WorkspaceFileExistence.UNKNOWN
    return RuntimeWorkspaceFileScope(existence, root_path)


async def resolve_runtime_workspace_file_scope(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
    entity_id: str | None,
    workspace_id: str | None,
    task_id: str | None,
) -> RuntimeWorkspaceFileScope | None:
    """Resolve one Workspace write against its immutable physical folder.

    Classification is synchronous, but Workspace storage identity lives in the
    database.  The approval middleware calls this resolver once before
    classification so task-less chat writes and task worker writes use the
    same exact physical scope instead of scanning every Workspace directory.
    This lookup is read-only: approval must never create a folder as a side
    effect.
    """

    from packages.core.ai.runtime.composite_tools import (
        RuntimeCompositeToolCallFactory,
    )

    canonical_call = RuntimeCompositeToolCallFactory.create(tool_name, arguments)
    normalized_tool = canonical_call.tool_name
    normalized_workspace_id = str(workspace_id or "").strip()
    normalized_entity_id = str(entity_id or "").strip()
    if normalized_tool == "code_project":
        if not normalized_workspace_id or not normalized_entity_id:
            return None
        return await _code_project_workspace_file_scope(
            normalized_entity_id,
            canonical_call.arguments,
        )
    if normalized_tool == "bash":
        command = str(canonical_call.arguments.get("command") or "")
        if _base_command(command) not in BashCommandSpecFactory.LOCAL_ONLY_COMMANDS or not bash_write_targets(command):
            return None
    from packages.core.ai.runtime.authorization_receipts import (
        WorkspaceFileAuthorizationResourceFactory,
    )

    if (
        not WorkspaceFileAuthorizationResourceFactory.supports_workspace_scope(
            normalized_tool
        )
        or not normalized_entity_id
    ):
        return None
    if not normalized_workspace_id:
        from packages.core.ai.runtime.media_outputs import prepare_runtime_media_output_plan
        from packages.core.services.entity_fs import get_entity_root

        media_plan = prepare_runtime_media_output_plan(
            tool_name=normalized_tool, arguments=canonical_call.arguments,
            artifact_base_dir="", entity_root=get_entity_root(normalized_entity_id),
        )
        return RuntimeWorkspaceFileScope(WorkspaceFileExistence.MISSING, "", media_plan) if media_plan else None

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.workspace import Workspace
    from packages.core.services.generated_media_naming import (
        current_workspace_artifact_scope,
        scope_workspace_artifact_path,
        workspace_execution_artifact_dir,
    )
    from packages.core.services.workspace_artifacts import (
        artifact_folder_id_from_entity_storage_path,
        workspace_artifact_storage_base,
    )
    from packages.core.services.workspace_layout import WorkspaceArtifactDir

    async with async_session() as db:
        # Lightweight unit-test adapters that only stand in for the later
        # authorization session deliberately have no query interface. Keep
        # the legacy exact task-path fallback for those adapters.
        if not hasattr(db, "execute"):
            return None
        artifact_folder_id = (await db.execute(
            select(Workspace.artifact_folder_id).where(
                Workspace.id == normalized_workspace_id,
                Workspace.entity_id == normalized_entity_id,
                Workspace.deleted_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()

    if not artifact_folder_id:
        return RuntimeWorkspaceFileScope(WorkspaceFileExistence.UNKNOWN)

    args = canonical_call.arguments
    raw_params = args.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    generate_file_workspace_asset = (
        normalized_tool == "generate_file"
        and str(args.get("kind") or "").strip().lower().replace("-", "_")
        == "image"
        and bool(params.get("workspace_asset_key"))
    )
    workspace_base = workspace_artifact_storage_base(str(artifact_folder_id))
    workspace_shared = (
        normalized_tool in {"write_file", "generate_file"}
        and str(args.get("storage_scope") or "task").strip().lower() == "workspace"
    ) or bool(args.get("workspace_shared")) or (
        normalized_tool == "generate_image"
        and bool(args.get("workspace_asset_key"))
    ) or generate_file_workspace_asset
    output_scope = current_workspace_artifact_scope(normalized_entity_id, normalized_workspace_id)
    from packages.core.services.entity_fs import get_entity_root
    artifact_base = workspace_execution_artifact_dir(
        workspace_base,
        task_id=None if workspace_shared else task_id,
        workflow_run_id=(output_scope.workflow_run_id if output_scope and not workspace_shared else None),
        workflow_lineage_root_run_id=(output_scope.workflow_lineage_root_run_id if output_scope and not workspace_shared else None),
        entity_root=get_entity_root(normalized_entity_id),
    )
    if normalized_tool == "archive_file_collection":
        collection = args.get("collection")
        files = collection.get("files") if isinstance(collection, dict) else None
        source_ids = [
            str(item.get("document_id") or "").strip()
            for item in files or []
            if isinstance(item, dict) and item.get("role") in {"source_audio", "source_cover"}
        ]
        if len(source_ids) != 2 or len(set(source_ids)) != 2:
            return RuntimeWorkspaceFileScope(WorkspaceFileExistence.UNKNOWN)
        from packages.core.models.document import Document
        async with async_session() as db:
            documents = list((await db.execute(select(Document).where(
                Document.entity_id == normalized_entity_id,
                Document.id.in_(source_ids),
                Document.is_trashed.is_(False),
            ))).scalars().all())
        documents_by_id = {document.id: document for document in documents}
        if set(documents_by_id) != set(source_ids):
            return RuntimeWorkspaceFileScope(WorkspaceFileExistence.UNKNOWN)
        lineage = str(output_scope.workflow_lineage_root_run_id if output_scope else "").strip()
        canonical_source_ids: dict[str, str] = {}
        for role in ("source_audio", "source_cover"):
            source_id = next(
                str(item.get("document_id") or "").strip()
                for item in files
                if isinstance(item, dict) and item.get("role") == role
            )
            metadata = documents_by_id[source_id].metadata_
            provenance = metadata.get("file_collection_archive") if isinstance(metadata, dict) else None
            canonical_source_ids[role] = (
                str(provenance.get("source_document_id") or "").strip()
                if isinstance(provenance, dict)
                and provenance.get("role") == role
                and provenance.get("workflow_lineage_root_run_id") == lineage
                else source_id
            )
        missing = set(canonical_source_ids.values()) - set(documents_by_id)
        if missing:
            async with async_session() as db:
                originals = list((await db.execute(select(Document).where(
                    Document.entity_id == normalized_entity_id,
                    Document.id.in_(missing),
                    Document.is_trashed.is_(False),
                ))).scalars().all())
            documents_by_id.update({document.id: document for document in originals})
        if any(
            documents_by_id.get(source_id) is None
            or not str(documents_by_id[source_id].fs_path or "")
            for source_id in canonical_source_ids.values()
        ):
            return RuntimeWorkspaceFileScope(WorkspaceFileExistence.UNKNOWN)
        from packages.core.services.file_collections import archive_file_collection_target_names
        names = archive_file_collection_target_names({
            role: str(documents_by_id[source_id].fs_path)
            for role, source_id in canonical_source_ids.items()
        })
        return RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.MISSING,
            artifact_base,
            file_targets=tuple(f"{artifact_base}/{names[role]}" for role in ("source_audio", "source_cover")),
        )
    requested_output = str(
        args.get("name") or args.get("output_name") or args.get("filename")
        or args.get("output_directory") or args.get("path") or args.get("target_path")
        or params.get("name") or params.get("output_name") or params.get("filename") or ""
    ).strip()
    requested_target = scope_workspace_artifact_path(requested_output, artifact_base)
    if (
        requested_target.startswith(workspace_base + "/")
        and not requested_target.startswith(artifact_base + "/")
    ):
        # Explicit Workspace destinations (including ledgers and canonical
        # sibling files) keep their identity even inside an execution archive.
        # Authorization below still binds exact resources, never the home tree.
        artifact_base = workspace_base

    from packages.core.ai.runtime.media_outputs import prepare_runtime_media_output_plan
    from packages.core.services.entity_fs import get_entity_root

    if normalized_tool == "bash":
        return RuntimeWorkspaceFileScope(WorkspaceFileExistence.UNKNOWN, artifact_base)

    media_plan = prepare_runtime_media_output_plan(
        tool_name=normalized_tool, arguments=args,
        artifact_base_dir=artifact_base, entity_root=get_entity_root(normalized_entity_id),
    )
    if media_plan is not None:
        return RuntimeWorkspaceFileScope(WorkspaceFileExistence.MISSING, artifact_base, media_plan)

    # Narration still resolves its voice-specific directory in the handler.
    # Retain its directory contract; ordinary media use the exact plan above.
    if (
        normalized_tool in {"generate_image", "generate_audio"}
        or generate_file_workspace_asset
    ):
        return RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.MISSING,
            artifact_base,
        )

    from packages.core.ai.runtime.authorization_receipts import (
        WorkspaceFileResourceMatchKind,
    )

    resources = ()
    from packages.core.services.generated_media_naming import workspace_artifact_default_dir
    typed_directories = workspace_artifact_default_dir(artifact_base, "documents") != artifact_base
    media_resources = WorkspaceFileAuthorizationResourceFactory.media_tool_resources(
        normalized_tool,
        args,
        artifact_base_dir=artifact_base,
        entity_id=normalized_entity_id,
    )
    if normalized_tool == "generate_document_file":
        resource = str(args.get("name") or "").strip() or None
        resources = WorkspaceFileAuthorizationResourceFactory.generate_document_resources(
            args,
            resource,
            workspace_scoped=typed_directories,
        )
    elif normalized_tool == "sandbox_save_result":
        resources = WorkspaceFileAuthorizationResourceFactory.sandbox_save_result_resources(
            args, typed_directories=typed_directories,
        )
    elif normalized_tool == "generate_file":
        resource = str(
            args.get("name")
            or params.get("name")
            or params.get("output_name")
            or params.get("filename")
            or ""
        ).strip() or None
        resources = WorkspaceFileAuthorizationResourceFactory.generate_file_resources(
            args,
            resource,
            typed_directories=typed_directories,
        )
    elif media_resources:
        resources = media_resources
    else:
        requested_path = str(
            args.get("target_path") if normalized_tool == "replace_file" else args.get("path")
            or ""
        ).strip()
        literal_path = _entity_abs_path(normalized_entity_id, requested_path)
        if literal_path and os.path.exists(literal_path):
            if _path_is_within_artifact_base(
                normalized_entity_id,
                requested_path,
                artifact_base,
            ):
                return RuntimeWorkspaceFileScope(
                    WorkspaceFileExistence.EXISTS,
                    artifact_base,
                )
            if artifact_base != workspace_base and _path_is_within_artifact_base(
                normalized_entity_id,
                requested_path,
                workspace_base,
            ):
                return RuntimeWorkspaceFileScope(
                    WorkspaceFileExistence.EXISTS,
                    workspace_base,
                )
            if artifact_folder_id_from_entity_storage_path(requested_path):
                # A Workspace-bound invocation may not downgrade a path in
                # another Workspace/task artifact root to an unbound entity
                # receipt. Keep the scope unresolved so commit fails closed.
                return RuntimeWorkspaceFileScope(
                    WorkspaceFileExistence.UNKNOWN
                )
            # An explicit existing entity path is already exact and does
            # not belong to this Workspace storage root. Leave it on the
            # exact entity-path receipt path; the Runtime mutation guard
            # revalidates its Knowledge ACL before consuming the receipt.
            return None
        candidate_bases = [artifact_base]
        if workspace_base and workspace_base != artifact_base:
            candidate_bases.append(workspace_base)
        for candidate_base in candidate_bases:
            scoped_path = scope_workspace_artifact_path(
                requested_path,
                candidate_base,
                default_subdir=WorkspaceArtifactDir.DOCUMENTS.value,
            )
            existence = _entity_file_exists(normalized_entity_id, scoped_path)
            if existence is True:
                return RuntimeWorkspaceFileScope(
                    WorkspaceFileExistence.EXISTS,
                    candidate_base,
                )
            if existence is None:
                return RuntimeWorkspaceFileScope(
                    WorkspaceFileExistence.UNKNOWN,
                    candidate_base,
                )
        return RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.MISSING,
            artifact_base,
        )

    if not resources:
        return RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.MISSING,
            artifact_base,
        )
    resource = resources[0]
    if resource.match_kind in {
        WorkspaceFileResourceMatchKind.TREE,
        WorkspaceFileResourceMatchKind.COLLISION_SAFE_FILE,
    }:
        # Unnamed/code generation creates collision-safe children beneath an
        # approved target rather than modifying the existing path itself.
        return RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.MISSING,
            artifact_base,
        )
    existence = _entity_file_exists(
        normalized_entity_id,
        scope_workspace_artifact_path(resource.resource_id, artifact_base),
    )
    return RuntimeWorkspaceFileScope(
        {
            True: WorkspaceFileExistence.EXISTS,
            False: WorkspaceFileExistence.MISSING,
            None: WorkspaceFileExistence.UNKNOWN,
        }[existence],
        artifact_base,
    )


def _path_is_within_artifact_base(
    entity_id: str,
    rel_path: str,
    artifact_base: str,
) -> bool:
    candidate = _entity_abs_path(entity_id, rel_path)
    base = _entity_abs_path(entity_id, artifact_base)
    return bool(
        candidate
        and base
        and (candidate == base or candidate.startswith(base + os.sep))
    )


def _workspace_write_file_existence(
    *,
    entity_id: str | None,
    workspace_id: str | None,
    task_id: str | None,
    path: str,
) -> WorkspaceFileExistence | None:
    if not workspace_id or not task_id or not path:
        return None
    literal_exists = _entity_file_exists(entity_id, path)
    if literal_exists is True:
        return WorkspaceFileExistence.EXISTS
    from packages.core.services.workspace_layout import WorkspaceArtifactDir

    normalized = str(path).strip().replace("\\", "/").strip("/")
    scoped_resource = (
        normalized
        if "/" in normalized
        else f"{WorkspaceArtifactDir.DOCUMENTS.value}/{normalized}"
    )
    return _workspace_task_artifact_existence(entity_id, task_id, scoped_resource)


def _classify_file_write(
    *,
    path: str,
    entity_id: str | None,
    title: str,
    default_key: str = "workspace.file.write",
    existence: WorkspaceFileExistence | None = None,
) -> RuntimeApprovalAction:
    rel_path = str(path or "").strip()
    resolved_existence = existence
    if resolved_existence is None:
        raw_exists = _entity_file_exists(entity_id, rel_path)
        resolved_existence = {
            True: WorkspaceFileExistence.EXISTS,
            False: WorkspaceFileExistence.MISSING,
            None: WorkspaceFileExistence.UNKNOWN,
        }[raw_exists]
    if resolved_existence is WorkspaceFileExistence.EXISTS:
        return RuntimeApprovalAction("action", "workspace.file.modify", "medium", title, "file", "modify", rel_path or None)
    if resolved_existence is WorkspaceFileExistence.MISSING:
        return RuntimeApprovalAction("action", "workspace.file.create", "medium", title, "file", "create", rel_path or None)
    return RuntimeApprovalAction("action", default_key, "medium", title, "file", "modify", rel_path or None)


def _classify_bash_tool(
    args: dict[str, Any], *, entity_id: str | None,
    workspace_file_scope: RuntimeWorkspaceFileScope | None = None,
) -> RuntimeApprovalAction | None:
    command = str(args.get("command") or "").strip()
    if workspace_file_scope and workspace_file_scope.artifact_base_dir and entity_id:
        from packages.core.ai.runtime.bash_output_paths import BashOutputPathError, scope_bash_output_command
        from packages.core.services.entity_fs import get_entity_root
        try:
            command = scope_bash_output_command(
                command, artifact_base_dir=workspace_file_scope.artifact_base_dir,
                entity_root=get_entity_root(entity_id),
            ).command
        except BashOutputPathError:
            return RuntimeApprovalAction("action", "workspace.file.write", "high", "write workspace file", "file", "modify")
    if not command:
        return RuntimeApprovalAction("action", "cli.exec", "low", "run a command", "cli", "execute")
    base_cmd = _base_command(command)
    if not base_cmd:
        return RuntimeApprovalAction("action", "cli.exec", "low", "run a command", "cli", "execute")

    if base_cmd in _FILE_DELETE_BASE_CMDS:
        return RuntimeApprovalAction("action", "workspace.file.delete", "high", "delete files from this workspace", "file", "delete")
    if _command_has_delete_hint(command):
        return RuntimeApprovalAction("action", "workspace.file.delete", "high", "delete files from this workspace", "file", "delete")
    if base_cmd in _FILE_MUTATION_BASE_CMDS:
        if base_cmd == "sed" and "-i" not in command:
            return RuntimeApprovalAction("action", "cli.exec", "low", "run a command", "cli", "execute")
        return RuntimeApprovalAction("action", "workspace.file.modify", "high", "modify files in this workspace", "file", "modify")
    if base_cmd in _FILE_CREATE_BASE_CMDS or _command_has_redirection(command):
        paths = bash_write_targets(command)
        if not paths:
            return RuntimeApprovalAction("action", "workspace.file.modify", "high", "modify files in this workspace", "file", "modify")
        existence = [_entity_file_exists(entity_id, path) for path in paths]
        if any(v is True for v in existence) or any(v is None for v in existence):
            return RuntimeApprovalAction("action", "workspace.file.modify", "high", "modify files in this workspace", "file", "modify")
        return RuntimeApprovalAction("action", "workspace.file.create", "medium", "create files in this workspace", "file", "create")
    if _command_has_write_hint(command):
        return RuntimeApprovalAction("action", "workspace.file.modify", "high", "modify files in this workspace", "file", "modify")
    return RuntimeApprovalAction("action", "cli.exec", "low", "run a command", "cli", "execute")


def _base_command(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return ""
    try:
        parts = shlex.split(stripped)
    except ValueError:
        parts = stripped.split()
    if not parts:
        return ""
    return parts[0].rsplit("/", 1)[-1]


def _command_has_write_hint(command: str) -> bool:
    stripped = f" {command.strip()} "
    return any(hint in stripped for hint in _CLI_WRITE_HINTS) or _command_has_redirection(command)


def _command_has_delete_hint(command: str) -> bool:
    stripped = command.strip()
    return bool(
        re.search(r"(?:^|[;&|]\s*)rm\s+", stripped)
        or re.search(r"\bxargs\s+rm\b", stripped)
        or re.search(r"\bfind\b.*(?:\s-delete\b|-exec\s+rm\b)", stripped)
    )


def _command_has_redirection(command: str) -> bool:
    return BashCommandSpecFactory.has_write_redirection(command)


def bash_write_targets(command: str) -> list[str]:
    """Best-effort write target extraction for simple CLI commands.

    Extracts the file paths a bash command will create, modify, or delete so
    the approval prompt can name them ("Modify paper.tex" / "Delete log.txt")
    instead of dumping the raw command at the user. Coverage focuses on the
    common shapes the runtime classifier already flags as workspace.file.*.
    Chained commands are split into simple shell segments; each segment is
    still parsed conservatively, so unknown segments simply add no paths.
    """
    stripped = command.strip()
    if not stripped:
        return []
    if _SHELL_SEGMENT_SEPARATOR_RE.search(stripped):
        segment_targets: list[str] = []
        for segment in re.split(r"\s+(?:&&|\|\||;|\|)\s+", stripped):
            for target in bash_write_targets(segment):
                if target not in segment_targets:
                    segment_targets.append(target)
        return segment_targets
    try:
        args = shlex.split(stripped)
    except ValueError:
        return []
    if not args:
        return []
    base_cmd = args[0].rsplit("/", 1)[-1]

    def non_flags(items: list[str]) -> list[str]:
        return [item for item in items if item and not item.startswith("-")]

    targets: list[str] = []
    if base_cmd in {"mkdir", "touch", "chmod"}:
        targets.extend(non_flags(args[1:]))
    elif base_cmd == "cp":
        clean = non_flags(args[1:])
        if len(clean) >= 2:
            targets.append(clean[-1])
    elif base_cmd == "mv":
        # `mv src dst` (2-arg form): dst is the file being created/overwritten.
        # `mv src1 src2 ... destdir/` (n-arg form): destdir is the directory
        # receiving the files. Either way the last non-flag arg is the target.
        clean = non_flags(args[1:])
        if len(clean) >= 2:
            targets.append(clean[-1])
    elif base_cmd == "rm":
        # `rm file1 file2 ...` deletes every non-flag arg.
        targets.extend(non_flags(args[1:]))
    elif base_cmd == "sed":
        # Only `sed -i ...` mutates files in place. Other sed invocations
        # stream to stdout and don't touch the filesystem; skip those.
        if any(part == "-i" or part.startswith("-i") for part in args[1:]):
            # After the script argument, remaining non-flag args are files
            # that sed will rewrite in place. Heuristic: collect everything
            # that doesn't look like a flag or the s/x/y/ script. We treat
            # the LAST run of non-flag args as the files (skipping the
            # script if it's the first non-flag).
            clean = non_flags(args[1:])
            if len(clean) >= 2:
                targets.extend(clean[1:])
    elif base_cmd == "tee":
        targets.extend(non_flags(args[1:]))

    for op in (">>", ">"):
        if op in args:
            idx = args.index(op)
            if idx + 1 < len(args):
                targets.append(args[idx + 1])
    targets.extend(BashCommandSpecFactory.write_redirection_targets(stripped))
    return list(dict.fromkeys(t for t in targets if t))


def _entity_file_exists(entity_id: str | None, rel_path: str) -> bool | None:
    abs_path = _entity_abs_path(entity_id, rel_path)
    if not abs_path:
        return None
    return os.path.exists(abs_path)


def _entity_abs_path(entity_id: str | None, rel_path: str) -> str | None:
    if not entity_id or not rel_path:
        return None
    try:
        from packages.core.config import get_settings

        settings = get_settings()
        if not settings.MANOR_FS_ENABLED:
            return None
        root = os.path.realpath(os.path.join(settings.MANOR_FS_ROOT, entity_id))
        candidate = os.path.realpath(os.path.join(root, rel_path.lstrip("/")))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate
    except Exception:
        return None


def split_mcp_tool(tool_name: str) -> tuple[str | None, str | None]:
    parts = str(tool_name or "").split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return None, None
    return parts[1], parts[2]


def _looks_public_or_paid(server: str, action: str, args: dict[str, Any]) -> bool:
    text = f"{server} {action} {json.dumps(args, ensure_ascii=False, default=str)[:500]}".lower()
    if server == "suno" and action in {"generate_music", "create_cover", "create_mashup"}:
        return True
    return any(word in text for word in ("publish", "post", "send", "payment", "charge", "refund", "invoice"))
