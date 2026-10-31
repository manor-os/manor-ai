"""The sandbox tool's side of the shared sandbox: never "busy", never silently lost.

* a command beyond the slot cap is queued by the runner; a caller with no
  durable run waits in-process and gets the result;
* every runner-reported end is explained accurately: not started (queue
  timeout), killed (out of memory / pids / disk), lost while running (not
  retried);
* a runner that restarts mid-command never leaves the call hanging: a short
  blip is ridden out, a longer outage answers "runner unreachable" -- "never
  sent" only when the request certainly never reached it;
* only an older runner without a slot queue still answers busy.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest


def _status(status: str, **extra):
    base = {
        "sandbox_id": "sb", "execution_id": "e", "status": status, "created_at": 0.0,
        "stdout": None, "stderr": None, "exit_code": None, "error": None,
        "outcome": None, "outcome_detail": None, "queue_position": None, "events": [],
        "terminal": status in {"completed", "failed", "cancelled"},
    }
    base.update(extra)
    return SimpleNamespace(**base)


class ScriptedClient:
    def __init__(self, start, statuses):
        self.start = start
        self.statuses = list(statuses)
        self.started_with: dict = {}
        self.cancelled: list[str] = []

    async def start_exec(self, **kwargs):
        self.started_with = kwargs
        if isinstance(self.start, Exception):
            raise self.start
        return self.start

    async def execution_status(self, sandbox_id, execution_id, **kwargs):
        item = self.statuses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def cancel_execution(self, sandbox_id, execution_id):
        self.cancelled.append(execution_id)

    async def close(self):
        return None


@pytest.fixture
def tool(monkeypatch):
    from packages.core.ai.tools import sandbox_tools
    from packages.core.services import sandbox_session_service
    from packages.core.services.sandbox_sdk.capabilities import capabilities_from_health
    from tests.sandbox_exec_fakes import CURRENT_RUNNER_HEALTH

    async def allow(**_kwargs):
        return None

    async def placement(*_args, **_kwargs):
        return {"workdir": "/workspace/c/conv/agents/main", "skill_env": False, "skill": ""}

    async def no_db(*_args, **_kwargs):
        return None

    async def current_runner(_sandbox_id):
        return capabilities_from_health(CURRENT_RUNNER_HEALTH)

    monkeypatch.setattr(sandbox_session_service, "runner_capabilities_for_sandbox", current_runner)
    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", allow)
    monkeypatch.setattr(sandbox_tools, "_agent_exec_placement", placement)
    monkeypatch.setattr(sandbox_tools, "_set_runtime_active_execution", no_db)
    monkeypatch.setattr(sandbox_tools, "_clear_runtime_active_execution", no_db)

    def run(client):
        async def get_client(_sandbox_id):
            return client

        monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", get_client)
        return sandbox_tools._sandbox_exec(
            entity_id="ent", user_id="user", conversation_id="conv",
            sandbox_id="sb", command="make", _agent_id_from_context="main",
        )

    return run


def _error(result: str) -> dict:
    return json.loads(result)["error"]


@pytest.mark.asyncio
async def test_a_queued_command_is_waited_for_and_its_result_returned(tool) -> None:
    client = ScriptedClient(
        _status("queued", queue_position=2),
        [_status("queued", queue_position=1), _status("running"), _status("completed", stdout="built", exit_code=0)],
    )
    assert await tool(client) == "built\n[exit_code: 0]"
    # Fair dispatch key and queue budget are sent with the work-area placement.
    assert client.started_with["fair_key"] == "conv"
    assert client.started_with["queue_timeout"] >= 1
    assert client.started_with["workdir"] == "/workspace/c/conv/agents/main"


@pytest.mark.asyncio
async def test_a_command_that_never_got_a_slot_is_reported_not_started(tool) -> None:
    client = ScriptedClient(
        _status("queued", queue_position=1),
        [_status("failed", outcome="queue_timeout", outcome_detail="waited")],
    )
    error = _error(await tool(client))
    assert error["code"] == "sandbox_execution_not_started"
    assert "Nothing ran" in error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,detail",
    [
        ("oom_killed", "killed: out of memory (the sandbox's memory limit is 1g)."),
        ("pids_killed", "killed: the command's processes exhausted the sandbox's process limit"),
        ("disk_killed", "killed: its work area /workspace/c/conv held 240 MB"),
    ],
)
async def test_a_killed_command_says_why(tool, outcome, detail) -> None:
    client = ScriptedClient(
        _status("running"),
        [_status("failed", exit_code=137, stderr="Killed", outcome=outcome, outcome_detail=detail)],
    )
    result = await tool(client)
    assert result.endswith(f"[{detail}]\n[exit_code: 137]")


@pytest.mark.asyncio
async def test_a_command_lost_with_its_container_is_reported_and_never_rerun(tool, monkeypatch) -> None:
    from packages.core.ai.tools import sandbox_tools

    forgotten = []

    async def forget(sandbox_id, *, scope=None):
        forgotten.append(sandbox_id)

    monkeypatch.setattr(sandbox_tools, "forget_vanished_sandbox", forget)
    client = ScriptedClient(
        _status("running"),
        [_status("failed", outcome="sandbox_lost", outcome_detail="lost while running", error="lost")],
    )
    error = _error(await tool(client))
    assert error["code"] == "sandbox_lost_while_running"
    assert "NOT retried" in error["message"]
    assert forgotten == ["sb"]


@pytest.mark.asyncio
async def test_a_runner_blip_is_ridden_out(tool) -> None:
    client = ScriptedClient(
        _status("running"),
        [httpx.RemoteProtocolError("server disconnected"), _status("completed", stdout="ok", exit_code=0)],
    )
    assert await tool(client) == "ok\n[exit_code: 0]"


@pytest.mark.asyncio
async def test_a_runner_that_stays_down_never_hangs_the_call(tool, monkeypatch) -> None:
    from packages.core.ai.tools import sandbox_tools

    monkeypatch.setattr(sandbox_tools, "_RUNNER_BLIP_SECONDS", 0.0)
    client = ScriptedClient(_status("running"), [httpx.ConnectError("connection refused")] * 3)
    error = _error(await tool(client))
    assert error["code"] == "sandbox_runner_unreachable"
    assert "may or may not have run" in error["message"]


@pytest.mark.asyncio
async def test_a_request_that_never_reached_the_runner_says_it_did_not_run(tool) -> None:
    error = _error(await tool(ScriptedClient(httpx.ConnectError("refused"), [])))
    assert error["code"] == "sandbox_runner_unreachable"
    assert "never sent, so it did not run" in error["message"]


@pytest.mark.asyncio
async def test_only_a_runner_without_a_queue_answers_busy(tool) -> None:
    from packages.core.services.sandbox_sdk.exceptions import SandboxError

    busy = SandboxError("Sandbox is already running 4 command(s)", status_code=409, code="sandbox_busy")
    error = _error(await tool(ScriptedClient(busy, [])))
    assert error["code"] == "sandbox_busy"


@pytest.mark.asyncio
async def test_a_command_of_a_work_area_over_its_disk_share_is_refused_as_not_started(tool) -> None:
    """The runner's disk quota refuses a full work area's commands -- at
    admission (409 ``sandbox_disk_quota``) or while it waited for a slot
    (outcome ``disk_quota``); either way the agent is told nothing ran and
    why, with a typed code."""

    from packages.core.services.sandbox_sdk.exceptions import SandboxError

    message = "Not started: work area /workspace/c/conv holds 240 MB of this user's shared /workspace"
    refused = SandboxError(message, status_code=409, code="sandbox_disk_quota", body={"started": False})
    at_admission = _error(await tool(ScriptedClient(refused, [])))
    while_waiting = _error(await tool(ScriptedClient(
        _status("queued", queue_position=1),
        [_status("failed", outcome="disk_quota", outcome_detail=message, error=message)],
    )))
    for error in (at_admission, while_waiting):
        assert error["code"] == "sandbox_disk_quota"
        assert error["message"].startswith(message) and "Nothing ran" in error["message"]
