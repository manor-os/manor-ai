"""The agent loop reuses the runtime's provider-approval continuation helpers."""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_agentic_loop_keeps_private_provider_continuation_aliases() -> None:
    from packages.core.ai.agentic_loop import (
        _pop_tool_continuation,
        _retry_call_after_tool_continuation,
    )
    from packages.core.ai.runtime.provider_approvals import (
        pop_tool_continuation,
        retry_call_after_tool_continuation,
    )

    assert _pop_tool_continuation is pop_tool_continuation
    assert (
        _retry_call_after_tool_continuation
        is retry_call_after_tool_continuation
    )
    assert _pop_tool_continuation(args={}) is None
