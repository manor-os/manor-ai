"""Runtime routing helpers for the user's local Chrome browser."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


GENERIC_WEB_TOOLS = frozenset({"web_search", "web_fetch", "browse_web"})

CHROME_MCP_TOOLS = frozenset({
    "mcp__chrome__status",
    "mcp__chrome__open",
    "mcp__chrome__navigate",
    "mcp__chrome__list_tabs",
    "mcp__chrome__claim_tab",
    "mcp__chrome__activate_tab",
    "mcp__chrome__close_tab",
    "mcp__chrome__ping_tab",
    "mcp__chrome__read_page",
    "mcp__chrome__computer",
    "mcp__chrome__wait",
    "mcp__chrome__get_web_content",
    "mcp__chrome__click_element",
    "mcp__chrome__hover",
    "mcp__chrome__fill_or_select",
    "mcp__chrome__scroll",
    "mcp__chrome__scroll_wheel",
    "mcp__chrome__click_point",
    "mcp__chrome__type_text",
    "mcp__chrome__press_key",
    "mcp__chrome__upload",
    "mcp__chrome__screenshot",
    "mcp__chrome__send_cdp",
    "mcp__chrome__inject_script",
    "mcp__chrome__set_cursor",
    "mcp__chrome__hide_cursor",
    "mcp__chrome__set_badge",
})

CHROME_LOCAL_BROWSER_DEFAULT_TOOLS = (
    "mcp__chrome__status",
    "mcp__chrome__open",
    "mcp__chrome__navigate",
    "mcp__chrome__list_tabs",
    "mcp__chrome__claim_tab",
    "mcp__chrome__read_page",
    "mcp__chrome__computer",
    "mcp__chrome__wait",
    "mcp__chrome__get_web_content",
    "mcp__chrome__click_element",
    "mcp__chrome__hover",
    "mcp__chrome__fill_or_select",
    "mcp__chrome__press_key",
    "mcp__chrome__scroll",
    "mcp__chrome__upload",
    "mcp__chrome__inject_script",
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
        return set(CHROME_MCP_TOOLS)

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
            "mcp-chrome-style mcp__chrome__open/select-tab -> "
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
    """Allow Chrome navigation URLs, including AI-composed search/result URLs."""
    return None


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

    if tool_name == "mcp__chrome__type_text":
        return json.dumps({
            "status": "blocked",
            "reason": "chrome_visible_locator_required",
            "blocked_tool": tool_name,
            "message": (
                "Chrome type_text inserts text into the currently focused "
                "element and cannot identify the target by itself. The latest "
                "workflow has no reliable focused editable node."
            ),
            "next_step": (
                "Call mcp__chrome__read_page to understand the page, then use "
                "the ref from chrome_read_page with mcp__chrome__fill_or_select. "
                "Press Enter or click a visible submit control only after the "
                "targeted fill succeeds."
            ),
        }, ensure_ascii=False)

    if tool_name in _CHROME_NODE_ACTION_TOOLS:
        ref = str(args.get("ref") or args.get("node_id") or args.get("nodeId") or "").strip()
        if not _CHROME_REF_RE.match(ref):
            return _blocked_invalid_chrome_ref(tool_name, "ref", ref)

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

    if tool_name == "mcp__chrome__press_key":
        key = str(args.get("key") or "").strip()
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

    return None


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
