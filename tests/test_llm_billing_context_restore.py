"""llm_billing_context must restore the previous billing scope without crashing.

Two guarantees:

1. **Accuracy (nesting):** exiting an inner billing scope restores the OUTER
   scope, so LLM calls after the inner scope are still attributed to the outer
   entity. Exiting the outermost scope restores "no scope".

2. **No cross-context crash:** the scope must tear down cleanly even when its
   exit runs in a different context than its entry (the Celery worker wraps
   each task in a fresh ``asyncio.run`` event loop / context). The old
   token-based ``reset()`` raised "Token was created in a different Context"
   there; restoring the previous value with ``set(prev)`` does not.
"""

from __future__ import annotations

import asyncio
import contextvars

from packages.core.ai.llm_client import llm_billing_context, _billing_ctx_var


def test_nested_billing_context_restores_outer_for_accuracy():
    async def run():
        assert _billing_ctx_var.get() is None
        async with llm_billing_context("entity_outer", source="a"):
            assert _billing_ctx_var.get().entity_id == "entity_outer"
            async with llm_billing_context("entity_inner", source="b"):
                assert _billing_ctx_var.get().entity_id == "entity_inner"
            # Inner scope ended — usage now must bill the OUTER entity again.
            assert _billing_ctx_var.get().entity_id == "entity_outer"
        # Outermost scope ended — no billing scope remains.
        assert _billing_ctx_var.get() is None

    asyncio.run(run())


def test_exit_in_separate_context_does_not_raise():
    # Enter in one context, then run the exit inside a *different* copied
    # context — the shape the Celery worker produces by wrapping each task in
    # its own asyncio.run. The old token-based reset() raised
    # "Token was created in a different Context" here.
    cm = llm_billing_context("entity_x", source="worker")

    enter_loop = asyncio.new_event_loop()
    try:
        enter_loop.run_until_complete(cm.__aenter__())
    finally:
        enter_loop.close()

    def _exit_in_fresh_context():
        exit_loop = asyncio.new_event_loop()
        try:
            exit_loop.run_until_complete(cm.__aexit__(None, None, None))
        finally:
            exit_loop.close()

    # Must not raise.
    contextvars.copy_context().run(_exit_in_fresh_context)
