"""No in-product tool is mistaken for a Chrome tool.

The Chrome runtime canonicalizes bare names ("computer", "click", ...) to its
own tools, so a Manor tool with such a name gets the Chrome control lease
injected into its arguments and the Chrome workflow guards around it: an
approved call is refused as "payload changed" when retried (its arguments
carry a new control_epoch), and a turn that finalized its Chrome tabs blocks
it.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def full_tool_pool(monkeypatch):
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools import register_all_tools
    from packages.core.config import get_settings

    # Sandbox tools register only when a sandbox service is configured.
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")
    monkeypatch.setenv("SANDBOX_SERVICE_URL", "http://sandbox-service")
    pool = ToolPool()
    register_all_tools(pool)
    return pool


def test_no_registered_tool_is_claimed_by_the_chrome_runtime(full_tool_pool) -> None:
    from packages.core.ai.runtime.chrome_routing import CHROME_MCP_TOOLS
    from packages.core.ai.runtime.chrome_tool_catalog import canonical_chrome_tool_name

    names = set(full_tool_pool.registered_tool_names())
    assert len(names) > 100  # the real registry, not an empty pool
    claimed = sorted(
        name for name in names
        if not name.startswith(("mcp__chrome__", "browser_", "chrome_"))
        and canonical_chrome_tool_name(name) in CHROME_MCP_TOOLS
    )
    assert claimed == []
