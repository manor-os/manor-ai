"""Every Sandbox command can be stopped, and stopping finds the command itself.

Review findings (2026-10-05):
- An execution id was sent only when a call had both a durable run id and a
  tool call id (not for task workers, nested Skill loops or non-durable chat).
  Without one the runner can only drop its ``docker exec`` client, so a
  timed-out command kept running while the tool said it "was cancelled".
- Cancelling a run looked for containers by the run that allocated them. A
  container reused from an earlier turn (warm reuse keeps the allocating
  run's row) or any container outside external-runner mode was never found,
  so Stop left the command running.
- With Redis unreachable the cancel path read "no warm marker" as "nobody
  reuses this container" and destroyed it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from tests.sandbox_exec_fakes import ExecViaStart  # noqa: E402

from packages.core.ai.tools import sandbox_tools


def _context(**fields):
    base = {"runtime_run_id": "", "runtime_tool_call_id": "", "runtime_tool_attempt": 1}
    base.update(fields)
    return SimpleNamespace(**base)


def test_every_command_is_named_and_a_replayable_call_keeps_its_name() -> None:
    durable = _context(runtime_run_id="run-1", runtime_tool_call_id="call-1")
    assert sandbox_tools._runtime_sandbox_execution_id(durable, "sbx") == (
        sandbox_tools._runtime_sandbox_execution_id(durable, "sbx")
    )
    # A task worker has a call id but no durable run id: still named, still stable.
    task = _context(runtime_tool_call_id="call-1")
    assert sandbox_tools._runtime_sandbox_execution_id(task, "sbx").startswith("manor-")
    assert sandbox_tools._runtime_sandbox_execution_id(task, "sbx") == (
        sandbox_tools._runtime_sandbox_execution_id(task, "sbx")
    )
    # No call id at all (a nested Skill loop strips it): a fresh name each time.
    bare = _context()
    first = sandbox_tools._runtime_sandbox_execution_id(bare, "sbx")
    assert first.startswith("manor-")
    assert first != sandbox_tools._runtime_sandbox_execution_id(bare, "sbx")
    # A resumed attempt is a new execution.
    assert sandbox_tools._runtime_sandbox_execution_id(durable, "sbx") != (
        sandbox_tools._runtime_sandbox_execution_id(
            _context(runtime_run_id="run-1", runtime_tool_call_id="call-1", runtime_tool_attempt=2), "sbx",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_error", "stopped"),
    [(None, True), (RuntimeError("runner unreachable"), False)],
)
async def test_a_timed_out_command_is_stopped_by_name_and_reported_truthfully(
    monkeypatch, cancel_error, stopped,
) -> None:
    calls: list[tuple[str, str | None]] = []

    class Client(ExecViaStart):
        async def exec(self, *, sandbox_id, command, timeout, execution_id=None, **_placement):
            calls.append(("exec", execution_id))
            raise httpx.ReadTimeout("timed out")

        async def cancel_execution(self, sandbox_id, execution_id):
            calls.append(("cancel", execution_id))
            if cancel_error is not None:
                raise cancel_error

        async def close(self):
            return None

    async def allow(**_kwargs):
        return None

    async def client_for(_sandbox_id):
        return Client()

    async def not_vanished(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", allow)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client_for)
    monkeypatch.setattr(sandbox_tools, "_vanished_sandbox_error", not_vanished)

    result = json.loads(await sandbox_tools._sandbox_exec(
        entity_id="e", user_id="u", sandbox_id="sbx-1", command="sleep 999", timeout=5,
    ))

    exec_id = calls[0][1]
    assert exec_id and exec_id.startswith("manor-"), "the command was sent with a name"
    assert ("cancel", exec_id) in calls, "the tool stops the command it named"
    message = result["error"]["message"]
    assert result["error"]["code"] == sandbox_tools.SandboxToolErrorCode.EXEC_TIMEOUT.value
    if stopped:
        assert "was stopped" in message
    else:
        assert "may still be running" in message and "was stopped" not in message


def _cancel_harness(monkeypatch, *, mode: str):
    from packages.core.services import runtime_run_service

    calls: list[str] = []

    class Client(ExecViaStart):
        def __init__(self, *, base_url, **_kwargs):
            self.base_url = base_url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def cancel_execution(self, sandbox_id, execution_id):
            calls.append(f"cancel:{self.base_url}:{sandbox_id}:{execution_id}")

        async def destroy(self, sandbox_id):
            calls.append(f"destroy:{sandbox_id}")

    class NoInstances:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, _statement):
            # The reused container's row belongs to the run that allocated it.
            return SimpleNamespace(all=lambda: [])

    async def runner_url(sandbox_id: str) -> str:
        return f"http://runner-of-{sandbox_id}:8000"

    monkeypatch.setattr("packages.core.database.async_session", lambda: NoInstances())
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(SANDBOX_COORDINATION_MODE=mode, SANDBOX_API_TOKEN="token"),
    )
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", Client)
    monkeypatch.setattr("packages.core.services.sandbox_session_service.sandbox_runner_base_url", runner_url)
    return runtime_run_service, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["external-runner", "local"])
async def test_cancel_stops_the_runs_own_command_on_a_reused_container(monkeypatch, mode) -> None:
    service, calls = _cancel_harness(monkeypatch, mode=mode)
    run = SimpleNamespace(
        id="run-turn-2",
        root_run_id="root-turn-2",
        active_sandbox_id="sbx-from-turn-1",
        active_execution_id="manor-exec-2",
    )

    await service.cancel_runtime_run_resources(run)

    assert calls == ["cancel:http://runner-of-sbx-from-turn-1:8000:sbx-from-turn-1:manor-exec-2"]


@pytest.mark.asyncio
async def test_an_unreadable_cache_never_reads_as_an_unclaimed_container(monkeypatch) -> None:
    from packages.core.services import sandbox_session_service as service

    async def no_marker(_sandbox_id):
        return None

    async def cache_down(_sandbox_id):
        return False

    async def cache_up(_sandbox_id):
        return True

    monkeypatch.setattr(service, "warm_registry_key_for", no_marker)
    monkeypatch.setattr(service, "_cache_reachable", cache_down)
    assert await service.is_sandbox_kept_warm("sbx-1") is True
    monkeypatch.setattr(service, "_cache_reachable", cache_up)
    assert await service.is_sandbox_kept_warm("sbx-1") is False


def test_an_expired_generic_sandbox_is_reopened_with_create_not_a_skill() -> None:
    generic = json.loads(sandbox_tools._sandbox_expired_error("sbx-1", sandbox_tools._GENERIC_SANDBOX_NAME, True))
    assert "sandbox action='create'" in generic["error"]["message"]
    assert "invoke_skill" not in generic["error"]["message"]
    skill = json.loads(sandbox_tools._sandbox_expired_error("sbx-2", "pptx", True))
    assert "invoke_skill(skill='pptx')" in skill["error"]["message"]
