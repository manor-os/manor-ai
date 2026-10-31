"""A local stand-in for one sandbox, for bash tool tests.

Commands run for real (``sh -c`` in a temp dir, each in a worker thread so
commands of several conversations run at the same time), so the mirror
scripts and the user command behave as they would in the shared container;
only paths are remapped: the shared ``/workspace`` tmpfs (where each agent's
working copy lives, ``/workspace/c/<area>/agents/<agent>/workspace``) and
``/tmp`` to temp dirs.

Like the runner, a command takes one of the sandbox's command slots
(``slots``; unlimited by default) or waits its turn in a FIFO for up to its
``queue_timeout``; side-channel execs (the mirror scripts) run next to the
slots. ``occupy``/``free`` let a test hold slots with other work.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from packages.core.ai.runtime import bash_mirror
from packages.core.ai.runtime.sandbox import runtime_sandbox_layout
from tests.sandbox_exec_fakes import CURRENT_RUNNER_HEALTH

_SANDBOX_PATH = re.compile(r"(?<![\w./-])/(workspace|skill|tmp)(?=/|\b)")


@dataclass
class _Execution:
    """A runner execution record (``ExecutionStatusResult``'s shape)."""

    sandbox_id: str
    execution_id: str
    command: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    error: str | None = None
    outcome: str | None = None
    outcome_detail: str | None = None
    queue_position: int | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}

    def view(self) -> SimpleNamespace:
        return SimpleNamespace(
            sandbox_id=self.sandbox_id, execution_id=self.execution_id, status=self.status,
            created_at=self.created_at, stdout=self.stdout, stderr=self.stderr,
            exit_code=self.exit_code, error=self.error, outcome=self.outcome,
            outcome_detail=self.outcome_detail, queue_position=self.queue_position,
            terminal=self.terminal,
        )


@dataclass
class LocalSandbox:
    base: Path
    calls: list[dict] = field(default_factory=list)
    envs: list[dict] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    slots: int | None = None
    running: set[str] = field(default_factory=set)
    waiting: collections.deque = field(default_factory=collections.deque)
    executions: dict[str, _Execution] = field(default_factory=dict)
    launched: list[str] = field(default_factory=list)
    # What the runner reports in /health (its capabilities).
    health: dict = field(default_factory=lambda: dict(CURRENT_RUNNER_HEALTH))

    def mirror_for(self, binding_key: str | None = "conv-1", agent_id: str | None = "agent-1") -> Path:
        """The local directory of one agent's working copy."""

        location = bash_mirror.MirrorLocation.for_layout(runtime_sandbox_layout(binding_key, agent_id))
        return Path(self._map(location.root))

    @property
    def mirror(self) -> Path:
        """The working copy the last command used (the default caller's before any)."""

        return Path(self._map(self.roots[-1])) if self.roots else self.mirror_for()

    @property
    def scratch(self) -> Path:
        return self.base / "tmp"

    def _map(self, text: str) -> str:
        # Paths already on the host -- the test's own temp dir, which holds
        # this sandbox's base and the entity filesystem -- are left as they
        # are: under Linux pytest's temp dir is itself under /tmp, and a test
        # that writes a Manor file from inside a command (a user's concurrent
        # save) must reach that file, not a path remapped into the sandbox.
        host = re.escape(str(self.base.parent))
        pattern = re.compile(rf"(?P<host>(?<![\w./-]){host}(?=/|\b)\S*)|{_SANDBOX_PATH.pattern}")
        return pattern.sub(
            lambda match: match.group("host") or f"{self.base}/{match.group(2)}",
            text,
        )

    async def write_file_base64(self, sandbox_id: str, path: str, content_base64: str, mkdir: bool = True):
        target = Path(self._map(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(content_base64)
        if target.name.startswith("manor-") and target.suffix == ".json":
            import json

            job = json.loads(data)
            if "root" in job:
                self.roots.append(job["root"])
            for key in ("root", "state"):
                if key in job:
                    job[key] = self._map(job[key])
            data = json.dumps(job).encode()
        target.write_bytes(data)
        return SimpleNamespace(path=path, written=True)

    # ── slots (the runner's fair FIFO, one work area) ──

    def occupy(self, name: str) -> None:
        """Other work of the user holds one command slot until ``free``."""
        self.running.add(name)

    def free(self, name: str) -> None:
        self.running.discard(name)
        self._dispatch()

    def lose_container(self) -> None:
        """The container dies with the runner still up: its records settle the runner's way.

        A queued command never started (``sandbox_lost`` / ``never started``);
        a running one is reported ``lost while running``.
        """
        for record in list(self.waiting):
            self.waiting.remove(record)
            record.status, record.outcome, record.outcome_detail = "failed", "sandbox_lost", "never started"
            record.error = "sandbox lost before the command started"
            record.done.set()
        for record in self.executions.values():
            if record.status == "running":
                record.status, record.outcome, record.outcome_detail = "failed", "sandbox_lost", "lost while running"
                record.error = "sandbox lost while the command ran"
                record.done.set()

    def queued_execution_ids(self) -> list[str]:
        return [record.execution_id for record in self.waiting]

    def _slot_free(self) -> bool:
        return self.slots is None or (len(self.running) < self.slots and not self.waiting)

    def _dispatch(self) -> None:
        while self.waiting and (self.slots is None or len(self.running) < self.slots):
            record = self.waiting.popleft()
            self._launch(record)
        for position, record in enumerate(self.waiting, start=1):
            record.queue_position = position

    def _launch(self, record: _Execution) -> None:
        self.running.add(record.execution_id)
        record.status = "running"
        record.queue_position = None
        self.launched.append(record.execution_id)
        spec = record._spec  # type: ignore[attr-defined]
        asyncio.get_running_loop().create_task(self._run_record(record, **spec))

    async def _run_record(self, record: _Execution, *, timeout, workdir, env) -> None:
        try:
            result = await self._run(record.command, timeout=timeout, workdir=workdir, env=env)
            if not record.terminal:  # settled meanwhile (its container was lost)
                record.stdout, record.stderr, record.exit_code = result.stdout, result.stderr, result.exit_code
                record.status = "completed" if result.exit_code == 0 else "failed"
        except _Timeout as exc:
            if not record.terminal:
                record.stdout, record.stderr = exc.stdout, exc.stderr
                record.status, record.error = "failed", f"Command timed out after {timeout}s"
        finally:
            record.done.set()
            self.running.discard(record.execution_id)
            self._dispatch()

    async def _queue_deadline(self, record: _Execution, queue_timeout: float) -> None:
        await asyncio.sleep(queue_timeout)
        if record in self.waiting:
            self.waiting.remove(record)
            record.status, record.outcome = "failed", "queue_timeout"
            record.error = record.outcome_detail = f"No slot freed up within {queue_timeout:.0f}s"
            record.done.set()
            self._dispatch()

    async def start_exec(
        self, sandbox_id, command, timeout=60, workdir=None, execution_id=None, env=None,
        fair_key=None, queue_timeout=None, skill=None, skill_env=None, _recorded=False,
    ):
        if not _recorded:
            self.calls.append({
                "command": command, "workdir": workdir, "env": dict(env or {}), "start": True,
                "fair_key": fair_key, "queue_timeout": queue_timeout, "skill": skill,
            })
            if env:
                self.envs.append(dict(env))
        execution_id = execution_id or uuid.uuid4().hex
        existing = self.executions.get(execution_id)
        if existing is not None:
            return existing.view()
        record = _Execution(sandbox_id=sandbox_id, execution_id=execution_id, command=command)
        record._spec = {"timeout": timeout, "workdir": workdir, "env": env}  # type: ignore[attr-defined]
        self.executions[execution_id] = record
        if self._slot_free():
            self._launch(record)
        elif not queue_timeout:
            from packages.core.services.sandbox_sdk.exceptions import SandboxError

            del self.executions[execution_id]
            raise SandboxError("sandbox busy", status_code=409, code="sandbox_busy")
        else:
            self.waiting.append(record)
            record.queue_position = len(self.waiting)
            asyncio.get_running_loop().create_task(self._queue_deadline(record, float(queue_timeout)))
        return record.view()

    async def execution_status(self, sandbox_id, execution_id, wait_seconds=0.0, **_):
        record = self.executions.get(execution_id)
        if record is None:
            from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

            raise SandboxNotFoundError(f"execution {execution_id} not found", status_code=404)
        if not record.terminal and wait_seconds:
            try:
                await asyncio.wait_for(record.done.wait(), timeout=float(wait_seconds))
            except asyncio.TimeoutError:
                pass
        return record.view()

    async def cancel_execution(self, sandbox_id, execution_id):
        record = self.executions.get(execution_id)
        cancelled = False
        if record is not None and record in self.waiting:
            self.waiting.remove(record)
            record.status, record.error = "cancelled", "cancelled before it started"
            record.done.set()
            self._dispatch()
            cancelled = True
        return SimpleNamespace(sandbox_id=sandbox_id, execution_id=execution_id, cancelled=cancelled)

    async def status(self, sandbox_id):
        return SimpleNamespace(sandbox_id=sandbox_id, status="running", queued_execution_ids=self.queued_execution_ids())

    async def destroy(self, sandbox_id):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def exec(self, sandbox_id, command, timeout=60, workdir=None, execution_id=None, env=None, **queue):
        self.calls.append({"command": command, "workdir": workdir, "env": dict(env or {}), **queue})
        if env:
            self.envs.append(dict(env))
        if self.slots is not None and not queue.get("side_channel"):
            # A command in the slot queue: wait for its turn like the runner does.
            started = await self.start_exec(
                sandbox_id, command, timeout=timeout, workdir=workdir, execution_id=execution_id,
                env=env, queue_timeout=queue.get("queue_timeout"), _recorded=True,
            )
            record = self.executions[started.execution_id]
            await record.done.wait()
            if record.outcome == "queue_timeout":
                from packages.core.services.sandbox_sdk.exceptions import SandboxError

                raise SandboxError(record.error or "queue timeout", status_code=409, code="sandbox_queue_timeout")
            return SimpleNamespace(stdout=record.stdout or "", stderr=record.stderr or "", exit_code=record.exit_code)
        try:
            return await self._run(command, timeout=timeout, workdir=workdir, env=env)
        except _Timeout as exc:
            from packages.core.services.sandbox_sdk.exceptions import SandboxTimeoutError

            raise SandboxTimeoutError(
                "timed out", status_code=504, body={"stdout": exc.stdout, "stderr": exc.stderr},
            )

    async def _run(self, command, *, timeout, workdir, env):
        mapped_env = {**os.environ, **{k: self._map(v) for k, v in (env or {}).items()}}
        cwd = self._map(workdir or "/tmp")
        os.makedirs(cwd, exist_ok=True)
        try:
            done = await asyncio.to_thread(
                subprocess.run,
                ["sh", "-c", self._map(command)], cwd=cwd, env=mapped_env,
                capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise _Timeout((exc.stdout or b"").decode(), (exc.stderr or b"").decode()) from None
        return SimpleNamespace(
            stdout=done.stdout.decode("utf-8", "replace"),
            stderr=done.stderr.decode("utf-8", "replace"),
            exit_code=done.returncode,
        )

    async def close(self):
        return None

    def user_commands(self) -> list[dict]:
        return [call for call in self.calls if "python3 -c" not in call["command"][:12]]


class _Timeout(Exception):
    def __init__(self, stdout: str, stderr: str) -> None:
        super().__init__("timed out")
        self.stdout, self.stderr = stdout, stderr


def install(monkeypatch, tmp_path: Path, sandbox_id: str | None = None) -> LocalSandbox:
    """Route the bash tool to one LocalSandbox."""

    from packages.core.ai.tools import bash_tool, sandbox_tools
    from packages.core.services import sandbox_session_service
    from packages.core.services.sandbox_sdk.capabilities import capabilities_from_health

    import uuid

    sandbox_id = sandbox_id or f"sbx-{uuid.uuid4().hex[:12]}"
    sandbox = LocalSandbox(base=tmp_path / "sandbox")
    sandbox.scratch.mkdir(parents=True, exist_ok=True)

    async def bound(entity_id, runtime_context):
        return sandbox_id, None

    async def client_for(_sandbox_id):
        return sandbox

    async def capabilities_for(_sandbox_id):
        return capabilities_from_health(sandbox.health)

    monkeypatch.setattr(sandbox_session_service, "runner_capabilities_for_sandbox", capabilities_for)
    monkeypatch.setattr(bash_tool, "_bash_sandbox", bound)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client_for)
    monkeypatch.setattr(sandbox_tools, "_sandbox_available", lambda: True)
    return sandbox
