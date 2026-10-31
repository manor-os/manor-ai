"""Chrome tool names shared by runtime routing and approval classification."""

from typing import Any

from packages.core.constants.chrome import CHROME_READ_ACTIONS as _CHROME_READ_ACTIONS

CHROME_MCP_TOOLS = frozenset({
    "mcp__chrome__documentation",
    "mcp__chrome__capabilities",
    "mcp__chrome__status",
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
    "mcp__chrome__close_tab",
    "mcp__chrome__close_tabs",
    "mcp__chrome__ping_tab",
    "mcp__chrome__read_page",
    "mcp__chrome__target_state",
    "mcp__chrome__resolve_target",
    "mcp__chrome__execute_cached_step",
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

# The paired-extension relay accepts only these read-only diagnostic CDP calls.
# Keep this catalog shared with approval classification so an allowed diagnostic
# does not require review before direct runtime dispatch rejects or accepts it.
CHROME_READ_ONLY_CDP_METHODS = frozenset({
    "DOM.getDocument",
    "DOM.describeNode",
    "DOM.getBoxModel",
    "Page.getFrameTree",
    "Page.getLayoutMetrics",
    "CSS.getComputedStyleForNode",
    "Accessibility.getFullAXTree",
})

# One interpretation of compatibility names and computer subactions. Keep the
# original call intact for authorization, approval hashes and exact resumes.
CHROME_TOOL_ALIASES = {
    "visible_dom": "read_page", "get_content": "get_web_content",
    "click": "click_element", "fill": "fill_or_select", "pageAssets": "page_assets",
    "get_interactive_elements": "read_page",
}
CHROME_COMPUTER_ACTIONS = {
    "left_click": "click_element", "click": "click_element", "double_click": "click_element",
    "fill": "fill_or_select", "type": "type_text", "key": "keyboard",
    "scroll": "scroll", "scroll_to": "scroll", "hover": "hover", "drag": "drag",
    "wait": "wait", "screenshot": "screenshot",
}
# Declared in a leaf module so governance.action_effects can use it too.
CHROME_READ_ACTIONS = _CHROME_READ_ACTIONS
CHROME_TARGET_ACTIONS = frozenset({
    "mcp__chrome__click_element", "mcp__chrome__fill_or_select", "mcp__chrome__click_point",
    "mcp__chrome__type_text", "mcp__chrome__press_key", "mcp__chrome__keyboard",
    "mcp__chrome__execute_cached_step",
    "mcp__chrome__download",
})


def canonical_chrome_tool_name(name: str) -> str:
    for prefix in ("mcp__chrome__", "browser_", "chrome_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return "mcp__chrome__" + CHROME_TOOL_ALIASES.get(name, name)


def normalize_chrome_call(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return the execution semantics, without mutating the signed call.

    Argument precedence mirrors BrowserController.computer/keySequenceParam.
    Unknown tools/subactions remain unknown; normalization never grants access.
    """
    name = canonical_chrome_tool_name(name)
    args = dict(arguments)
    if name == "mcp__chrome__computer":
        action = args.get("action")
        canonical = CHROME_COMPUTER_ACTIONS.get(action) if isinstance(action, str) else None
        if canonical is None:
            return name, args
        name = "mcp__chrome__" + canonical
        if canonical == "click_element":
            args["double"] = action == "double_click"
        elif canonical == "keyboard":
            text = args.get("text")
            if isinstance(text, str) and text.strip():
                args["key"] = text
            args.pop("text", None)
        elif canonical == "scroll":
            amount = args.get("scrollAmount", 3)
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                direction = args.get("scrollDirection") or "down"
                delta = amount * 300 * (-1 if direction in {"up", "left"} else 1)
                args["deltaX"] = delta if direction in {"left", "right"} else 0
                args["deltaY"] = 0 if direction in {"left", "right"} else delta
    return name, args


def chrome_key_sequence(args: dict[str, Any]) -> tuple[str, ...]:
    keys = args.get("keys")
    if isinstance(keys, list):
        keys = [key for key in keys if isinstance(key, str) and key.strip()]
    else:
        keys = []
    if not keys:
        raw = args.get("key")
        if not isinstance(raw, str) or not raw.strip():
            raw = args.get("keys")
        if not isinstance(raw, str) or not raw.strip():
            return ()
        keys = [raw] if raw == "+" else [key.strip() for key in raw.split("+") if key.strip()]
    if len(keys) > 5:
        return ()
    aliases = {"ctrl": "control", "cmd": "meta", "command": "meta", "option": "alt", "esc": "escape"}
    return tuple(aliases.get(key.strip().lower(), key.strip().lower()) for key in keys)


def chrome_navigation_key(args: dict[str, Any]) -> bool:
    keys = chrome_key_sequence(args)
    return (
        len(keys) == 1 and keys[0] in {
            "escape", "tab", "arrowdown", "arrowup", "arrowleft", "arrowright",
            "home", "end", "pageup", "pagedown",
        }
    ) or keys == ("shift", "tab") or (
        len(keys) == 2 and keys[0] == "control" and keys[1] in {"home", "end"}
    )
