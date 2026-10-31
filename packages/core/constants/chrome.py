"""Chrome action vocabulary shared by the runtime and the approval declarations.

A leaf module: ``governance.action_effects`` declares the basic Chrome reads
from this set, and must never import the ``packages.core.ai.runtime`` package
(whose ``__init__`` loads the approval classifier, which imports
``action_effects``).
"""
from __future__ import annotations

# Chrome actions that only observe the page, tab or target state.
CHROME_READ_ACTIONS = frozenset({
    "documentation", "capabilities", "status", "selected_tab", "tab_info",
    "list_tabs", "get_windows_and_tabs", "get_group_state", "ping_tab",
    "read_page", "target_state", "resolve_target", "wait", "get_interactive_elements",
    "inspect_selector", "get_web_content", "screenshot", "viewport", "page_assets", "console_logs", "js_dialog",
})
