from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SANDBOX_SERVICE_ROOT = Path(__file__).resolve().parents[1] / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE_ROOT))

from sandbox.models import (  # noqa: E402
    ContainerConfig,
    ExecResponse,
    SandboxStatus,
)
from sandbox.skill_runner import SkillRunner  # noqa: E402
from sandbox.docker_backend import DockerSandbox, _SANDBOX_BRIDGE_SOURCE  # noqa: E402
from sandbox.credential_refs import validate_sandbox_credential_ref  # noqa: E402
from packages.core.services.sandbox_credential_refs import (  # noqa: E402
    issue_sandbox_credential_ref,
)


def test_sandbox_credential_reference_expires_and_is_event_bound() -> None:
    credential_ref = issue_sandbox_credential_ref(
        signing_key="test-signing-key",
        sandbox_id="sandbox-1",
        execution_id="execution-1",
        event_id="credential-1",
        provider="example",
        integration_account_id="account-1",
        now=100,
    )
    expected = {
        "reference": credential_ref,
        "signing_key": "test-signing-key",
        "sandbox_id": "sandbox-1",
        "execution_id": "execution-1",
        "provider": "example",
        "integration_account_id": "account-1",
    }

    validate_sandbox_credential_ref(
        **expected,
        event_id="credential-1",
        now=219,
    )
    with pytest.raises(ValueError, match="scope does not match"):
        validate_sandbox_credential_ref(
            **expected,
            event_id="credential-2",
            now=219,
        )
    with pytest.raises(ValueError, match="expired"):
        validate_sandbox_credential_ref(
            **expected,
            event_id="credential-1",
            now=220,
        )


@pytest.mark.asyncio
async def test_idempotent_create_deduplicates_and_reopens_after_destroy(monkeypatch, tmp_path):
    import sandbox.skill_runner as skill_runner_module

    skill_dir = tmp_path / "idempotent-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Idempotent Skill\n", encoding="utf-8")

    setup_started = asyncio.Event()
    allow_first_setup = asyncio.Event()
    setup_calls = 0

    class FakeDockerSandbox:
        def __init__(self, sandbox_id, config):
            self.sandbox_id = sandbox_id
            self.config = config
            self.container_name = f"test-{sandbox_id}"
            self.status = SandboxStatus.CREATING
            self.skill = None

        async def setup(self, skill, env, auto_install=True):
            nonlocal setup_calls
            setup_calls += 1
            if setup_calls == 1:
                setup_started.set()
                await allow_first_setup.wait()
            self.skill = skill
            self.status = SandboxStatus.READY

        async def destroy(self):
            self.status = SandboxStatus.DESTROYED

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 2)
    monkeypatch.setattr(skill_runner_module, "DockerSandbox", FakeDockerSandbox)

    runner = SkillRunner()
    first_task = asyncio.create_task(
        runner.create_sandbox(
            str(skill_dir),
            env={},
            auto_install=False,
            idempotency_key="reservation-1",
        )
    )
    await setup_started.wait()
    follower_task = asyncio.create_task(
        runner.create_sandbox(
            str(skill_dir),
            env={},
            auto_install=False,
            idempotency_key="reservation-1",
        )
    )
    await asyncio.sleep(0)
    allow_first_setup.set()

    first, follower = await asyncio.gather(first_task, follower_task)

    assert first.sandbox_id == follower.sandbox_id
    assert setup_calls == 1
    assert runner._local_active_capacity.reserved == 1

    await runner.destroy_sandbox(first.sandbox_id)
    recreated = await runner.create_sandbox(
        str(skill_dir),
        env={},
        auto_install=False,
        idempotency_key="reservation-1",
    )
    other = await runner.create_sandbox(
        str(skill_dir),
        env={},
        auto_install=False,
        idempotency_key="reservation-2",
    )

    assert recreated.sandbox_id == first.sandbox_id
    assert other.sandbox_id != first.sandbox_id
    assert setup_calls == 3
    assert runner._local_active_capacity.reserved == 2


@pytest.mark.asyncio
async def test_execution_cancel_terminates_active_process_group_only_when_needed(monkeypatch):
    import sandbox.docker_backend as docker_backend_module

    execution_started = threading.Event()
    allow_execution_finish = threading.Event()
    signals: list[str] = []
    stubborn = True

    def fake_run_docker(args, **kwargs):
        nonlocal stubborn
        command = args[-1]
        if "start_new_session=True" in " ".join(args):
            execution_started.set()
            assert allow_execution_finish.wait(timeout=1)
            return subprocess.CompletedProcess(args, 137, stdout=b"", stderr=b"killed")
        if command.startswith("cat "):
            return subprocess.CompletedProcess(args, 0, stdout=b"321\n", stderr=b"")
        if "kill -TERM" in command:
            signals.append("TERM")
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if "kill -0" in command:
            return subprocess.CompletedProcess(
                args,
                0 if stubborn else 1,
                stdout=b"",
                stderr=b"",
            )
        if "kill -KILL" in command:
            signals.append("KILL")
            allow_execution_finish.set()
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if command.startswith("rm -f "):
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"Unexpected Docker command: {args}")

    monkeypatch.setattr(docker_backend_module, "_run_docker", fake_run_docker)
    monkeypatch.setattr(docker_backend_module, "_CANCEL_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(docker_backend_module, "_CANCEL_POLL_INTERVAL_SECONDS", 0.001)

    sandbox = DockerSandbox("sandbox-1", ContainerConfig())
    sandbox.container_name = "test-sandbox-1"
    sandbox.status = SandboxStatus.READY
    execution_task = asyncio.create_task(
        sandbox.exec("sleep 60", execution_id="execution-1")
    )
    assert await asyncio.to_thread(execution_started.wait, 1) is True

    assert await sandbox.cancel_execution("execution-1") is True
    result = await execution_task

    assert result.execution_id == "execution-1"
    assert signals == ["TERM", "KILL"]
    assert await sandbox.cancel_execution("execution-1") is False
    assert sandbox.active_execution_id is None

    signals.clear()
    stubborn = False
    sandbox._active_execution_id = "execution-2"
    assert await sandbox.cancel_execution("execution-2") is True
    assert signals == ["TERM"]


@pytest.mark.asyncio
async def test_background_execution_exposes_running_terminal_and_cancelled_status() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class FakeSandbox:
        status = SandboxStatus.READY

        def ensure_available(self):
            return None

        async def exec(self, command, timeout, workdir, execution_id, interactive=False):
            assert command == "sleep 60"
            assert timeout == 60
            assert workdir is None
            assert interactive is True
            started.set()
            await release.wait()
            return ExecResponse(
                stdout="",
                stderr="cancelled",
                exit_code=137,
                execution_id=execution_id,
            )

        async def cancel_execution(self, execution_id):
            assert execution_id == "execution-1"
            release.set()
            # Reproduce Docker's real ordering: the process task may observe
            # its exit before the signalling request returns to the runner.
            await asyncio.sleep(0)
            return True

    runner = SkillRunner()
    runner._sandboxes["sandbox-1"] = FakeSandbox()

    queued = await runner.start_execution(
        "sandbox-1",
        "sleep 60",
        execution_id="execution-1",
    )
    assert queued.status.value == "queued"
    await started.wait()

    running = await runner.get_execution_status("sandbox-1", "execution-1")
    assert running.status.value == "running"
    assert running.started_at is not None
    assert running.finished_at is None

    assert await runner.cancel_execution("sandbox-1", "execution-1") is True
    for _ in range(20):
        terminal = await runner.get_execution_status("sandbox-1", "execution-1")
        if terminal.status.value == "cancelled":
            break
        await asyncio.sleep(0)

    assert terminal.status.value == "cancelled"
    assert terminal.exit_code == 137
    assert terminal.finished_at is not None


@pytest.mark.asyncio
async def test_start_execution_deduplicates_while_mailbox_cleanup_is_blocked() -> None:
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    execution_started = asyncio.Event()
    allow_execution = asyncio.Event()
    cleanup_calls = 0
    execution_calls = 0

    class FakeSandbox:
        status = SandboxStatus.READY

        def ensure_available(self):
            return None

        async def exec(self, *_args, execution_id, **_kwargs):
            nonlocal execution_calls
            execution_calls += 1
            execution_started.set()
            await allow_execution.wait()
            return ExecResponse(
                stdout="done",
                stderr="",
                exit_code=0,
                execution_id=execution_id,
            )

    async def blocked_cleanup(_cleanup_ids) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_started.set()
        await allow_cleanup.wait()

    runner = SkillRunner()
    runner._sandboxes["sandbox-1"] = FakeSandbox()
    runner._cleanup_execution_mailboxes = blocked_cleanup

    first_task = asyncio.create_task(
        runner.start_execution(
            "sandbox-1",
            "echo done",
            execution_id="execution-shared",
        )
    )
    await cleanup_started.wait()
    second = await asyncio.wait_for(
        runner.start_execution(
            "sandbox-1",
            "echo done",
            execution_id="execution-shared",
        ),
        timeout=0.1,
    )
    allow_cleanup.set()
    first = await first_task
    await execution_started.wait()

    assert first.execution_id == second.execution_id == "execution-shared"
    assert cleanup_calls == 1
    assert execution_calls == 1

    allow_execution.set()
    execution_task = runner._execution_tasks[("sandbox-1", "execution-shared")]
    await execution_task


@pytest.mark.asyncio
async def test_start_execution_rejects_reused_id_for_different_request() -> None:
    allow_execution = asyncio.Event()

    class FakeSandbox:
        status = SandboxStatus.READY

        def ensure_available(self):
            return None

        async def exec(self, *_args, execution_id, **_kwargs):
            await allow_execution.wait()
            return ExecResponse(
                stdout="done",
                stderr="",
                exit_code=0,
                execution_id=execution_id,
            )

    runner = SkillRunner()
    runner._sandboxes["sandbox-1"] = FakeSandbox()
    await runner.start_execution(
        "sandbox-1",
        "echo one",
        execution_id="execution-shared",
    )

    with pytest.raises(ValueError, match="different request"):
        await runner.start_execution(
            "sandbox-1",
            "echo two",
            execution_id="execution-shared",
        )

    allow_execution.set()
    await runner._execution_tasks[("sandbox-1", "execution-shared")]


@pytest.mark.asyncio
async def test_background_execution_cancel_before_process_start_cleans_state() -> None:
    class FakeSandbox:
        status = SandboxStatus.READY

        def ensure_available(self):
            return None

        async def exec(self, *_args, **_kwargs):
            raise AssertionError("a pre-start cancellation must not execute")

        async def cancel_execution(self, _execution_id):
            return False

    runner = SkillRunner()
    runner._sandboxes["sandbox-1"] = FakeSandbox()
    await runner.start_execution(
        "sandbox-1",
        "echo should-not-run",
        execution_id="execution-before-start",
    )

    assert await runner.cancel_execution(
        "sandbox-1",
        "execution-before-start",
    ) is True
    terminal = await runner.get_execution_status(
        "sandbox-1",
        "execution-before-start",
    )

    assert terminal.status.value == "cancelled"
    assert runner._execution_cancel_requested == set()
    assert runner._execution_cancel_confirmed == set()


@pytest.mark.asyncio
async def test_background_nonzero_exit_is_a_failed_terminal_state() -> None:
    class FakeSandbox:
        status = SandboxStatus.READY

        def ensure_available(self):
            return None

        async def exec(self, *_args, execution_id, **_kwargs):
            return ExecResponse(
                stdout="",
                stderr="bad input",
                exit_code=2,
                execution_id=execution_id,
            )

        async def cancel_execution(self, _execution_id):
            return False

    runner = SkillRunner()
    runner._sandboxes["sandbox-1"] = FakeSandbox()
    await runner.start_execution(
        "sandbox-1",
        "exit 2",
        execution_id="execution-failed",
    )
    for _ in range(20):
        terminal = await runner.get_execution_status("sandbox-1", "execution-failed")
        if terminal.status.value == "failed":
            break
        await asyncio.sleep(0)

    assert terminal.status.value == "failed"
    assert terminal.exit_code == 2
    assert terminal.error == "command exited with code 2"


@pytest.mark.asyncio
async def test_background_execution_event_response_resumes_same_process() -> None:
    response_written = asyncio.Event()
    allow_complete = asyncio.Event()
    response_payloads: list[dict] = []

    class FakeSandbox:
        status = SandboxStatus.READY

        def ensure_available(self):
            return None

        async def exec(self, *_args, execution_id, **_kwargs):
            await response_written.wait()
            await allow_complete.wait()
            return ExecResponse(
                stdout="resumed\n",
                stderr="",
                exit_code=0,
                execution_id=execution_id,
            )

        async def cancel_execution(self, _execution_id):
            return False

        async def read_execution_event_lines(self, execution_id):
            assert execution_id == "execution-interactive"
            return [
                json.dumps(
                    {
                        "event_id": "tool-request-1",
                        "type": "need_tool",
                        "message": "Resolve a governed capability",
                        "payload": {"capability": "web.safe_search"},
                    }
                )
            ], False

        async def write_execution_response(self, execution_id, event_id, encoded):
            assert execution_id == "execution-interactive"
            assert event_id == "tool-request-1"
            response_payloads.append(json.loads(encoded))
            response_written.set()

    runner = SkillRunner()
    runner._sandboxes["sandbox-1"] = FakeSandbox()
    await runner.start_execution(
        "sandbox-1",
        "python interactive.py",
        execution_id="execution-interactive",
    )

    status = await runner.get_execution_status(
        "sandbox-1",
        "execution-interactive",
    )
    assert status.waiting_for_response is True
    assert status.next_sequence == 1
    assert status.events[0].type.value == "need_tool"
    assert status.events[0].requires_response is True

    with pytest.raises(KeyError, match="Execution event not found"):
        await runner.send_execution_response(
            "sandbox-1",
            "execution-interactive",
            "unknown-event",
            payload={},
            message="",
        )

    accepted = await runner.send_execution_response(
        "sandbox-1",
        "execution-interactive",
        "tool-request-1",
        payload={"result_ref": "tool-result-1"},
        message="Resolved through the Runtime gate",
    )
    duplicate = await runner.send_execution_response(
        "sandbox-1",
        "execution-interactive",
        "tool-request-1",
        payload={"result_ref": "tool-result-1"},
        message="Resolved through the Runtime gate",
    )
    assert accepted.accepted is True
    assert accepted.duplicate is False
    assert duplicate.duplicate is True
    assert len(response_payloads) == 1
    assert response_payloads[0]["payload"] == {"result_ref": "tool-result-1"}
    with pytest.raises(ValueError, match="different response"):
        await runner.send_execution_response(
            "sandbox-1",
            "execution-interactive",
            "tool-request-1",
            payload={"result_ref": "tool-result-2"},
            message="Resolved through the Runtime gate",
        )

    incremental = await runner.get_execution_status(
        "sandbox-1",
        "execution-interactive",
        after_sequence=1,
    )
    assert incremental.events == []
    assert incremental.next_sequence == 1
    assert incremental.waiting_for_response is False

    allow_complete.set()
    for _ in range(20):
        terminal = await runner.get_execution_status(
            "sandbox-1",
            "execution-interactive",
        )
        if terminal.status.value == "completed":
            break
        await asyncio.sleep(0)
    assert terminal.status.value == "completed"
    assert terminal.stdout == "resumed\n"
    terminal_retry = await runner.send_execution_response(
        "sandbox-1",
        "execution-interactive",
        "tool-request-1",
        payload={"result_ref": "tool-result-1"},
        message="Resolved through the Runtime gate",
    )
    assert terminal_retry.duplicate is True


@pytest.mark.asyncio
async def test_execution_response_size_is_bounded_before_docker_io() -> None:
    sandbox = DockerSandbox("sandbox-1", ContainerConfig())
    with pytest.raises(ValueError, match="exceeds 16384 bytes"):
        await sandbox.write_execution_response(
            "execution-1",
            "input-1",
            b"x" * (16 * 1024 + 1),
        )


@pytest.mark.asyncio
async def test_execution_response_rejects_terminal_transition_during_refresh() -> None:
    allow_execution_complete = asyncio.Event()
    refresh_started = asyncio.Event()
    allow_refresh_complete = asyncio.Event()
    response_writes = 0

    class FakeSandbox:
        status = SandboxStatus.READY

        def ensure_available(self):
            return None

        async def exec(self, *_args, execution_id, **_kwargs):
            await allow_execution_complete.wait()
            return ExecResponse(
                stdout="done",
                stderr="",
                exit_code=0,
                execution_id=execution_id,
            )

        async def read_execution_event_lines(self, _execution_id):
            refresh_started.set()
            await allow_refresh_complete.wait()
            return [
                json.dumps(
                    {
                        "event_id": "input-1",
                        "type": "need_input",
                        "message": "Choose",
                        "payload": {},
                    }
                )
            ], False

        async def write_execution_response(self, *_args):
            nonlocal response_writes
            response_writes += 1

    runner = SkillRunner()
    runner._sandboxes["sandbox-1"] = FakeSandbox()
    await runner.start_execution(
        "sandbox-1",
        "echo done",
        execution_id="execution-race",
    )
    response_task = asyncio.create_task(
        runner.send_execution_response(
            "sandbox-1",
            "execution-race",
            "input-1",
            payload={"choice": "a"},
            message="",
        )
    )
    await refresh_started.wait()
    execution_task = runner._execution_tasks[("sandbox-1", "execution-race")]
    allow_execution_complete.set()
    await execution_task
    allow_refresh_complete.set()

    with pytest.raises(RuntimeError, match="terminal sandbox execution"):
        await response_task
    assert response_writes == 0


@pytest.mark.asyncio
async def test_execution_response_rejects_secret_values(monkeypatch) -> None:
    import sandbox.skill_runner as skill_runner_module

    responses: list[dict] = []

    class FakeSandbox:
        status = SandboxStatus.READY

        def ensure_available(self):
            return None

        async def exec(self, *_args, **_kwargs):
            await asyncio.sleep(30)

        async def cancel_execution(self, _execution_id):
            return False

        async def read_execution_event_lines(self, _execution_id):
            return [
                json.dumps(
                    {
                        "event_id": "credential-request-1",
                        "type": "need_credential",
                        "message": "Credential reference required",
                        "payload": {"provider": "example"},
                    }
                )
            ], False

        async def write_execution_response(self, _execution_id, _event_id, encoded):
            responses.append(json.loads(encoded))

    runner = SkillRunner()
    runner._sandboxes["sandbox-1"] = FakeSandbox()
    monkeypatch.setattr(skill_runner_module.app_config, "API_TOKEN", "test-signing-key")
    record = await runner.start_execution(
        "sandbox-1",
        "sleep 30",
        execution_id="execution-secret",
    )
    await runner.get_execution_status("sandbox-1", record.execution_id)
    for payload in (
        {"access_token": "plaintext"},
        {"clientSecret": "plaintext"},
        {"Authorization": "Bearer plaintext-token"},
    ):
        with pytest.raises(ValueError, match="credential references"):
            await runner.send_execution_response(
                "sandbox-1",
                record.execution_id,
                "credential-request-1",
                payload=payload,
                message="",
            )
    with pytest.raises(ValueError, match="signed credential_ref"):
        await runner.send_execution_response(
            "sandbox-1",
            record.execution_id,
            "credential-request-1",
            payload={"credential_ref": "vault:credential-1"},
            message="plaintext credentials are not accepted here",
        )
    with pytest.raises(ValueError, match="reference fields only"):
        await runner.send_execution_response(
            "sandbox-1",
            record.execution_id,
            "credential-request-1",
            payload={"credential_ref": "vault:credential-1", "value": "plaintext"},
            message="",
        )
    with pytest.raises(ValueError, match="reference fields only"):
        await runner.send_execution_response(
            "sandbox-1",
            record.execution_id,
            "credential-request-1",
            payload={"credential_ref": "vault:credential-1", "provider": {"id": "example"}},
            message="",
        )

    with pytest.raises(ValueError, match="Invalid Sandbox credential reference"):
        await runner.send_execution_response(
            "sandbox-1",
            record.execution_id,
            "credential-request-1",
            payload={
                "credential_ref": "vault:credential-1",
                "integration_account_id": "account-1",
                "provider": "example",
            },
            message="",
        )

    credential_ref = issue_sandbox_credential_ref(
        signing_key="test-signing-key",
        sandbox_id="sandbox-1",
        execution_id=record.execution_id,
        event_id="credential-request-1",
        provider="example",
        integration_account_id="account-1",
    )
    with pytest.raises(ValueError, match="scope does not match"):
        await runner.send_execution_response(
            "sandbox-1",
            record.execution_id,
            "credential-request-1",
            payload={
                "credential_ref": credential_ref,
                "integration_account_id": "account-2",
                "provider": "example",
            },
            message="",
        )
    accepted = await runner.send_execution_response(
        "sandbox-1",
        record.execution_id,
        "credential-request-1",
        payload={
            "credential_ref": credential_ref,
            "integration_account_id": "account-1",
            "provider": "example",
        },
        message="",
    )
    assert accepted.accepted is True
    assert responses[0]["payload"] == {
        "credential_ref": credential_ref,
        "integration_account_id": "account-1",
        "provider": "example",
    }
    await runner.cancel_execution("sandbox-1", record.execution_id)
    task = runner._execution_tasks.get(("sandbox-1", record.execution_id))
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_execution_status_filters_secret_events_that_bypass_the_bridge() -> None:
    class FakeSandbox:
        status = SandboxStatus.READY

        def ensure_available(self):
            return None

        async def exec(self, *_args, **_kwargs):
            await asyncio.sleep(30)

        async def cancel_execution(self, _execution_id):
            return False

        async def read_execution_event_lines(self, _execution_id):
            return [
                json.dumps(
                    {
                        "event_id": "secret-key",
                        "type": "need_credential",
                        "message": "Credential required",
                        "payload": {"clientSecret": "plaintext"},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "secret-message",
                        "type": "warning",
                        "message": "Authorization: Bearer plaintext-token",
                        "payload": {},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "safe-event",
                        "type": "need_input",
                        "message": "Choose an option",
                        "payload": {"options": ["a", "b"]},
                    }
                ),
            ], False

    runner = SkillRunner()
    runner._sandboxes["sandbox-1"] = FakeSandbox()
    record = await runner.start_execution(
        "sandbox-1",
        "sleep 30",
        execution_id="execution-filtered",
    )

    status = await runner.get_execution_status("sandbox-1", record.execution_id)

    assert [event.event_id for event in status.events] == ["safe-event"]
    task = runner._execution_tasks.get(("sandbox-1", record.execution_id))
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_sandbox_bridge_rejects_secret_keys_and_free_text(tmp_path) -> None:
    bridge_path = tmp_path / "manor_sandbox_bridge.py"
    bridge_path.write_text(_SANDBOX_BRIDGE_SOURCE, encoding="utf-8")
    response_dir = tmp_path / "responses"
    response_dir.mkdir()
    env = {
        **os.environ,
        "MANOR_SANDBOX_EVENT_FILE": str(tmp_path / "events.jsonl"),
        "MANOR_SANDBOX_RESPONSE_DIR": str(response_dir),
    }
    cases = (
        ("", {"clientSecret": "plaintext"}),
        ("", {"Authorization": "Bearer plaintext-token"}),
        ("client_secret=plaintext", {"provider": "example"}),
        ("access_token=plaintext", {"provider": "example"}),
        ("refreshToken=plaintext", {"provider": "example"}),
        ("client-secret=plaintext", {"provider": "example"}),
    )

    for message, payload in cases:
        result = subprocess.run(
            [
                sys.executable,
                str(bridge_path),
                "emit",
                "--type",
                "need_credential",
                "--message",
                message,
                "--payload",
                json.dumps(payload),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "references only" in result.stderr


@pytest.mark.asyncio
async def test_startup_reconciles_unaddressable_managed_containers(monkeypatch):
    import sandbox.skill_runner as skill_runner_module

    removed: list[str] = []
    monkeypatch.setattr(
        skill_runner_module,
        "list_managed_sandbox_containers",
        lambda: ["container-a", "container-b"],
        raising=False,
    )
    monkeypatch.setattr(
        skill_runner_module,
        "remove_managed_sandbox_container",
        removed.append,
        raising=False,
    )

    runner = SkillRunner()
    await runner.startup()
    try:
        assert removed == ["container-a", "container-b"]
        assert runner.runtime_status()["orphan_cleanup_failures"] == []
    finally:
        await runner.shutdown()


@pytest.mark.asyncio
async def test_capacity_status_reports_stable_local_counters(monkeypatch):
    import sandbox.skill_runner as skill_runner_module

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 5)
    monkeypatch.setattr(skill_runner_module.app_config, "INSTANCE_MAX_EXECUTING", 2)
    monkeypatch.setattr(skill_runner_module, "_image_exists_locally", lambda _image: True)

    runner = SkillRunner()
    execute_lease = await runner._gate.acquire_execute()

    assert runner.runtime_status() == {
        "sandbox_image": skill_runner_module.app_config.SANDBOX_IMAGE,
        "sandbox_image_available": True,
        "active_sandboxes": 0,
        "reserved_active": 0,
        "max_active": 5,
        "executing": 1,
        "max_executing": 2,
        "orphan_cleanup_failures": [],
    }

    await execute_lease.release()
    assert runner.runtime_status()["executing"] == 0


def test_capacity_status_uses_active_limit_when_execute_limit_is_disabled(monkeypatch):
    import sandbox.skill_runner as skill_runner_module

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 5)
    monkeypatch.setattr(skill_runner_module.app_config, "INSTANCE_MAX_EXECUTING", 0)
    monkeypatch.setattr(skill_runner_module, "_image_exists_locally", lambda _image: True)

    runner = SkillRunner()

    assert runner.runtime_status()["max_executing"] == 5
