"""A real sandbox runner, in process, over a scripted Docker host.

The control plane talks to the REAL runner (``sandbox-service``: routes,
``SkillRunner``, ``DockerSandbox`` with its slot queue, Skill slots, error
mapping) through the real ``SandboxClient`` over an in-process ASGI
transport. Only the Docker CLI is scripted (``FakeDockerHost``): containers
can be created, started, killed and removed, and a launcher-wrapped command
blocks until the test releases it (or its container dies), so tests decide
exactly when slots free up.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

import httpx

from tests.sandbox_runner_fakes import API_TOKEN, completed, load_service_main

import sandbox.docker_backend as docker_backend  # noqa: E402
from api import routes as runner_routes  # noqa: E402


class FakeDockerHost:
    """Scripted ``docker`` CLI for many containers on one host."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.containers: dict[str, dict] = {}
        self.started: dict[str, threading.Event] = defaultdict(threading.Event)
        self.released: dict[str, threading.Event] = defaultdict(threading.Event)
        self.killed: dict[str, threading.Event] = defaultdict(threading.Event)
        self.start_order: list[str] = []
        self.pid_by_file: dict[str, int] = {}
        self.command_by_pid: dict[int, str] = {}
        self.container_of_command: dict[str, str] = {}
        self.launches: list[dict] = []
        self._next_pid = 1000

    # ── test controls ──

    def release(self, command: str) -> None:
        self.released[command].set()

    def kill_container(self, name: str) -> None:
        """The container dies (OOM-killed host, daemon restart): it stops running."""
        with self.lock:
            self.containers[name]["running"] = False
            victims = [command for command, owner in self.container_of_command.items() if owner == name]
        for command in victims:
            self.killed[command].set()

    def running_containers(self) -> list[str]:
        with self.lock:
            return [name for name, state in self.containers.items() if state["running"]]

    def wait_started(self, command: str, timeout: float = 10.0) -> bool:
        return self.started[command].wait(timeout)

    # ── the docker CLI ──

    def __call__(self, args, *, input_data=None, timeout=120, allow_failure=False):
        verb = args[1]
        if verb == "image":
            return completed(args, 0, stdout=b"[]")
        if verb == "inspect":
            name = args[-1]
            with self.lock:
                state = self.containers.get(name)
            if state is None:
                return completed(args, 1, stderr=f"Error: No such object: {name}".encode())
            return completed(args, 0, stdout=b"true\n" if state["running"] else b"false\n")
        if verb == "create":
            name = args[args.index("--name") + 1]
            with self.lock:
                self.containers[name] = {"running": False, "args": list(args)}
            return completed(args)
        if verb == "start":
            with self.lock:
                self.containers[args[-1]]["running"] = True
            return completed(args)
        if verb == "rm":
            name = args[-1]
            with self.lock:
                self.containers.pop(name, None)
                victims = [command for command, owner in self.container_of_command.items() if owner == name]
            for command in victims:
                self.killed[command].set()
            return completed(args)
        if verb == "ps":
            with self.lock:
                names = [name for name, state in self.containers.items() if state["running"]]
            return completed(args, 0, stdout=("\n".join(names) + "\n").encode() if "--format" in args else b"")
        if verb == "volume":
            return completed(args, 0, stdout=b"")
        if verb != "exec":
            raise AssertionError(f"unexpected docker call {args}")
        container = self._exec_container(args)
        with self.lock:
            state = self.containers.get(container)
        if state is None:
            return completed(args, 1, stderr=f"Error response from daemon: No such container: {container}".encode())
        if not state["running"]:
            return completed(args, 1, stderr=f"Error response from daemon: container {container} is not running".encode())
        if "-d" in args:
            return completed(args)  # the watchdog
        joined = " ".join(args)
        if "start_new_session=True" in joined:
            return self._launcher(args, container)
        command = args[-1]
        if command.startswith("cat /tmp/manor-exec"):
            with self.lock:
                pid = self.pid_by_file.get(command.split()[-1])
            if pid is None:
                return completed(args, 1, stderr=b"cat: no such file")
            return completed(args, 0, stdout=str(pid).encode())
        if command.startswith("kill -0"):
            return completed(args, 1)
        if command.startswith("kill -"):
            pid = int(command.split("-")[-1])
            with self.lock:
                target = self.command_by_pid.get(pid)
            if target is not None:
                self.killed[target].set()
            return completed(args)
        if "pathlib" in joined:
            return completed(args, 0, stdout=b"")
        return completed(args)

    def _exec_container(self, args: list[str]) -> str:
        index = 2
        while index < len(args) and args[index].startswith("-"):
            # flags with a value
            if args[index] in {"-w", "--env-file", "-e", "--env", "-u"}:
                index += 2
            else:
                index += 1
        return args[index]

    def _launcher(self, args: list[str], container: str):
        index = args.index("-c")
        command, pid_file = args[index + 2], args[index + 3]
        execution_id = args[index + 8]
        cwd = args[index + 10] if len(args) > index + 10 else ""
        skill_home = args[index + 13] if len(args) > index + 13 else ""
        with self.lock:
            self._next_pid += 1
            pid = self._next_pid
            self.pid_by_file[pid_file] = pid
            self.command_by_pid[pid] = command
            self.container_of_command[command] = container
            self.start_order.append(command)
            self.launches.append({
                "command": command, "execution_id": execution_id, "cwd": cwd,
                "skill_home": skill_home, "env_file": "--env-file" in args, "container": container,
            })
        self.started[command].set()
        deadline = time.monotonic() + 60
        while not (self.released[command].is_set() or self.killed[command].is_set()):
            if time.monotonic() > deadline:
                raise AssertionError(f"command {command!r} was never released")
            time.sleep(0.005)
        with self.lock:
            alive = self.containers.get(container, {}).get("running", False)
        if self.killed[command].is_set():
            return completed(args, 137 if not alive else 143)
        return completed(args, 0, stdout=f"out:{command}\n".encode())


def install_inprocess_runner(monkeypatch, name: str = "sandbox_main_inprocess"):
    """Run the real sandbox-service app in process; route every SandboxClient to it.

    Returns ``(runner, docker_host)``.
    """

    import packages.core.services.sandbox_sdk as sandbox_sdk
    from packages.core.services.sandbox_sdk.client import SandboxClient as RealClient

    host = FakeDockerHost()
    monkeypatch.setattr(docker_backend, "_run_docker", host)
    monkeypatch.setattr(docker_backend, "_CANCEL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(docker_backend, "_CANCEL_POLL_INTERVAL_SECONDS", 0.005)
    main = load_service_main(monkeypatch, name)
    runner_routes.set_runner(main.runner)
    app = main.app

    class InProcessSandboxClient(RealClient):
        def __init__(self, base_url: str = "http://runner", timeout: float = 180.0, api_token: str | None = None, **_):
            super().__init__(
                base_url=base_url or "http://runner",
                timeout=timeout,
                api_token=API_TOKEN,
                transport=httpx.ASGITransport(app=app),
            )

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", InProcessSandboxClient)
    return main.runner, host
