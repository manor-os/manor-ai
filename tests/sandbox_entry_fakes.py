"""An in-memory sandbox runner behind a fake ``SandboxClient`` class.

Patch ``packages.core.services.sandbox_sdk.SandboxClient`` with
``runner.client_class`` and every client the session service or the scheduler
constructs talks to this one runner: containers are created, executed in,
read, written and destroyed against shared state, and every call is recorded
so a test can prove which path created and removed a container.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import time
import uuid
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

ExecHandler = Callable[[str, str, dict[str, Any]], Awaitable[Any] | Any]


class FakeRunner:
    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.created: list[dict[str, Any]] = []
        self.destroyed: list[str] = []
        self.cancelled: list[tuple[str, str]] = []
        self.executions: dict[tuple[str, str], Any] = {}
        self.create_failures: list[Exception] = []
        self.exec_handler: ExecHandler | None = None
        # Like a real runner: when set, /health reports the host's CPUs and
        # (with ``reports_config``) a create answers with the effective
        # config, CPUs clamped to the host.
        self.host_cpus: float | None = None
        self.reports_config = True
        self._ids = itertools.count(1)
        # Ids never repeat across runs: instance rows in a reused test
        # database outlive the test that created them.
        self.token = uuid.uuid4().hex[:8]
        self.client_class = self._make_client_class()

    def live(self) -> set[str]:
        return set(self.containers)

    def _make_client_class(self):
        runner = self

        class FakeSandboxClient:
            def __init__(self, base_url: str = "", timeout: float = 0.0, api_token: str | None = None, **_kw):
                self.base_url = base_url
                runner.calls.append(("init", {"base_url": base_url, "api_token": api_token}))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                await self.close()

            async def close(self) -> None:
                runner.calls.append(("close", {}))

            async def health(self) -> dict:
                report = {
                    "sandbox_image_available": True,
                    "env_injection": "exec",
                    "active_sandboxes": len(runner.containers),
                }
                if runner.host_cpus is not None:
                    report["host_cpus"] = runner.host_cpus
                return report

            async def _create(self, source: str, kwargs: dict[str, Any]):
                runner.calls.append(("create", {"source": source, **kwargs}))
                if runner.create_failures:
                    raise runner.create_failures.pop(0)
                idem = kwargs.get("idempotency_key")
                if idem:
                    for sandbox_id, container in runner.containers.items():
                        if container.get("idempotency_key") == idem:
                            return self._created(sandbox_id)
                sandbox_id = f"fake-sbx-{runner.token}-{next(runner._ids)}"
                runner.containers[sandbox_id] = {
                    "skill_name": kwargs.get("skill_name"),
                    "config": dict(kwargs.get("config") or {}),
                    "env": dict(kwargs.get("env") or {}),
                    "files": {f"/skill/{name}": content.encode() for name, content in (kwargs.get("files") or {}).items()},
                    "idempotency_key": idem,
                    "base_url": self.base_url,
                    "created_at": time.time(),
                    "executing": None,
                }
                runner.created.append({"sandbox_id": sandbox_id, "source": source, **kwargs})
                return self._created(sandbox_id)

            def _created(self, sandbox_id: str):
                effective: dict[str, Any] = {}
                if runner.host_cpus is not None and runner.reports_config:
                    config = dict(runner.containers[sandbox_id]["config"])
                    if "cpus" in config:
                        config["cpus"] = min(float(config["cpus"]), float(runner.host_cpus))
                    effective["config"] = config
                return SimpleNamespace(
                    **effective,
                    sandbox_id=sandbox_id,
                    container_name=f"fake-{sandbox_id}",
                    status="ready",
                    workdir="/skill",
                    skill=SimpleNamespace(scripts=[], entry_hint=None, requirements_txt=None),
                    env_blocked=[],
                    env_injection="exec",
                )

            async def create_from_files(self, **kwargs):
                return await self._create("workspace", kwargs)

            async def create_from_builtin(self, **kwargs):
                return await self._create("builtin", kwargs)

            def _container(self, sandbox_id: str) -> dict[str, Any]:
                container = runner.containers.get(sandbox_id)
                if container is None:
                    raise SandboxNotFoundError(f"Sandbox not found: {sandbox_id}")
                return container

            def _info(self, sandbox_id: str):
                container = self._container(sandbox_id)
                return SimpleNamespace(
                    sandbox_id=sandbox_id,
                    container_name=f"fake-{sandbox_id}",
                    status="ready",
                    skill_name=container["skill_name"],
                    workdir="/skill",
                    created_at=container["created_at"],
                    last_used_at=time.time(),
                    config=container["config"],
                    active_command=None,
                    active_execution_id=container["executing"],
                    expires_at=time.time() + 600,
                )

            async def status(self, sandbox_id: str):
                return self._info(sandbox_id)

            async def touch(self, sandbox_id: str):
                runner.calls.append(("touch", {"sandbox_id": sandbox_id}))
                return self._info(sandbox_id)

            async def list(self):
                return [self._info(sandbox_id) for sandbox_id in list(runner.containers)]

            async def destroy(self, sandbox_id: str) -> None:
                runner.calls.append(("destroy", {"sandbox_id": sandbox_id}))
                self._container(sandbox_id)
                runner.containers.pop(sandbox_id, None)
                runner.destroyed.append(sandbox_id)

            async def exec(self, sandbox_id: str, command: str, timeout: int = 60, workdir: str | None = None,
                           execution_id: str | None = None, **_kw):
                container = self._container(sandbox_id)
                runner.calls.append(("exec", {
                    "sandbox_id": sandbox_id, "command": command, "timeout": timeout,
                    "workdir": workdir, "execution_id": execution_id, **_kw,
                }))
                container["executing"] = execution_id
                try:
                    if runner.exec_handler is not None:
                        result = runner.exec_handler(sandbox_id, command, container)
                        if asyncio.iscoroutine(result):
                            result = await result
                        if result is not None:
                            return result
                    return SimpleNamespace(stdout="", stderr="", exit_code=0)
                finally:
                    if sandbox_id in runner.containers:
                        runner.containers[sandbox_id]["executing"] = None

            async def start_exec(self, sandbox_id: str, command: str, timeout: int = 60, workdir: str | None = None,
                                 execution_id: str | None = None, **_kw):
                # Recorded as an "exec" like the blocking call: the command runs
                # to its end here, and its first status is already the final one.
                result = await self.exec(
                    sandbox_id, command, timeout=timeout, workdir=workdir, execution_id=execution_id, **_kw,
                )
                exit_code = getattr(result, "exit_code", 0)
                status = SimpleNamespace(
                    sandbox_id=sandbox_id, execution_id=execution_id or "",
                    status="completed" if exit_code == 0 else "failed", terminal=True,
                    stdout=getattr(result, "stdout", ""), stderr=getattr(result, "stderr", ""),
                    exit_code=exit_code, error=None, events=list(getattr(result, "events", None) or []),
                    next_sequence=len(getattr(result, "events", None) or []), queue_position=None,
                    outcome=getattr(result, "outcome", None), outcome_detail=getattr(result, "outcome_detail", None),
                )
                runner.executions[(sandbox_id, execution_id or "")] = status
                return status

            async def execution_status(self, sandbox_id: str, execution_id: str, **_kw):
                return runner.executions[(sandbox_id, execution_id)]

            async def cancel_execution(self, sandbox_id: str, execution_id: str):
                runner.cancelled.append((sandbox_id, execution_id))

            async def write_file(self, sandbox_id: str, path: str, content: str, mkdir: bool = True):
                self._container(sandbox_id)["files"][path] = content.encode("utf-8")
                return SimpleNamespace(path=path, written=True)

            async def write_file_base64(self, sandbox_id: str, path: str, content_base64: str, mkdir: bool = True):
                self._container(sandbox_id)["files"][path] = base64.b64decode(content_base64)
                return SimpleNamespace(path=path, written=True)

            async def read_file_base64(self, sandbox_id: str, path: str, max_size: int | None = None):
                content = self._container(sandbox_id)["files"][path]
                return SimpleNamespace(content_base64=base64.b64encode(content).decode("ascii"), size=len(content))

        return FakeSandboxClient
