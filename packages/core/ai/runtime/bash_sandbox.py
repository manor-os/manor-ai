"""Runtime boundary between the ``bash`` tool and the generic sandbox.

Tools never import each other directly; ``bash`` reaches the sandbox tool's
admission and execution helpers through these thin wrappers. Each resolves
its target at call time, so the generic sandbox stays the single
implementation (and tests patch it in one place).
"""

from __future__ import annotations

from typing import Any


def runtime_sandbox_available() -> bool:
    from packages.core.ai.tools import sandbox_tools

    return sandbox_tools._sandbox_available()


async def runtime_acquire_generic_sandbox(*, scope: Any, runtime_context: Any) -> Any:
    """The caller's general sandbox (the owner's, shared by all of its work)."""

    from packages.core.ai.tools import sandbox_tools

    return await sandbox_tools.acquire_generic_sandbox(scope=scope, runtime_context=runtime_context)


async def runtime_sandbox_client_for(sandbox_id: str) -> Any:
    from packages.core.ai.tools import sandbox_tools

    return await sandbox_tools._get_client_for_sandbox(sandbox_id)


def runtime_sandbox_execution_id(runtime_context: Any, sandbox_id: str) -> str:
    from packages.core.ai.tools import sandbox_tools

    return sandbox_tools._runtime_sandbox_execution_id(runtime_context, sandbox_id)


async def runtime_set_sandbox_active_execution(
    runtime_context: Any, *, sandbox_id: str, execution_id: str,
) -> None:
    from packages.core.ai.tools import sandbox_tools

    await sandbox_tools._set_runtime_active_execution(
        runtime_context, sandbox_id=sandbox_id, execution_id=execution_id,
    )


async def runtime_clear_sandbox_active_execution(runtime_context: Any, execution_id: str) -> None:
    from packages.core.ai.tools import sandbox_tools

    await sandbox_tools._clear_runtime_active_execution(runtime_context, execution_id)


# ── Exec-slot parking (the generic sandbox's, shared) ──
#
# A durable run whose bash command finds every command slot of the user's
# shared sandbox taken parks exactly like the ``sandbox`` tool's exec
# (``waiting_resource`` / ``sandbox_exec_slot``): the command waits its turn
# in the runner's fair FIFO, the scheduler's exec-slot sweep wakes the run
# once it left the queue, and the resumed call collects that same execution.


def runtime_sandbox_parks_on_slots(runtime_context: Any) -> bool:
    """True when this call's run can park on a slot instead of waiting in-process."""

    from packages.core.ai.tools import sandbox_tools

    return sandbox_tools._sandbox_parks_on_slots(runtime_context)


def runtime_exec_queue_wait_seconds() -> int:
    """How long a command may wait for a slot (``SANDBOX_EXEC_QUEUE_WAIT_SECONDS``)."""

    from packages.core.ai.tools import sandbox_tools

    return sandbox_tools._exec_queue_wait_seconds()


async def runtime_parked_exec_slot(runtime_context: Any) -> dict[str, Any] | None:
    """The ticket payload of the command this tool call parked on, if any (not consumed)."""

    from packages.core.ai.tools import sandbox_tools

    return await sandbox_tools._exec_slot_ticket(runtime_context, None)


async def runtime_park_on_exec_slot(
    runtime_context: Any,
    *,
    sandbox_id: str,
    execution_id: str,
    wait_seconds: int,
    continuation: dict[str, Any] | None = None,
) -> Any | None:
    """The Runtime suspension of a parked command, or None (then wait in-process)."""

    from packages.core.ai.tools import sandbox_tools

    return await sandbox_tools._park_on_exec_slot(
        runtime_context,
        sandbox_id=sandbox_id,
        execution_id=execution_id,
        wait_seconds=wait_seconds,
        continuation=continuation,
    )


async def runtime_consume_exec_slot_wait(runtime_context: Any) -> None:
    from packages.core.ai.tools import sandbox_tools

    await sandbox_tools._consume_exec_slot_wait(runtime_context)


async def runtime_await_sandbox_execution(
    client: Any,
    *,
    sandbox_id: str,
    execution_id: str,
    first: Any,
    budget_seconds: float,
    runtime_context: Any = None,
) -> tuple[Any, str]:
    """Long-poll one execution until it ends: ``(final status, last status seen)``.

    With a ``runtime_context`` inside a Workflow step, the command's progress
    is reported on that step while it runs.
    """

    from packages.core.ai.tools import sandbox_tools
    from packages.core.services.runtime_activity import sandbox_activity_recorder

    return await sandbox_tools._await_execution(
        client,
        sandbox_id=sandbox_id,
        execution_id=execution_id,
        first=first,
        budget_seconds=budget_seconds,
        on_status=sandbox_activity_recorder(runtime_context),
    )


async def runtime_stop_sandbox_execution(sandbox_id: str, execution_id: str) -> bool:
    """Stop (or withdraw from the slot queue) one command; True when confirmed."""

    from packages.core.ai.tools import sandbox_tools

    return await sandbox_tools._stop_execution(sandbox_id, execution_id)
