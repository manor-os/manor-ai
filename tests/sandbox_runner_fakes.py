"""Shared fakes for Sandbox runner tests: a scripted Docker CLI and a registered sandbox.

``FakeDocker`` stands in for ``docker_backend._run_docker`` and models one
container's lifecycle closely enough to exercise the real DockerSandbox and
SkillRunner code: a launcher-wrapped command that can block until released,
killed or removed; pid-file reads and process-group signals; side-channel file
commands; inspect/rm with the daemon's real error texts.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path

SANDBOX_SERVICE_ROOT = Path(__file__).resolve().parent.parent / "sandbox-service"
if str(SANDBOX_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SANDBOX_SERVICE_ROOT))
sys.modules.pop("config", None)

from sandbox.docker_backend import DockerCommandTimeout, DockerSandbox  # noqa: E402
from sandbox.fs_bridge import FsBridge  # noqa: E402
from sandbox.models import ContainerConfig, SandboxStatus, SkillManifest  # noqa: E402

API_TOKEN = "runner-test-secret"
NOT_RUNNING = b"Error response from daemon: container skill-sbx-demo is not running"


def completed(args, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.exists = True
        self.running = True
        self.rm_fails = False
        # Launcher-wrapped commands block until released, killed or removed
        # when ``block`` is set.
        self.block = False
        self.release = threading.Event()
        self.killed = threading.Event()
        self.launcher_started = threading.Event()
        self.launcher_result = (0, b"ok\n", b"")
        self.launcher_timeout: DockerCommandTimeout | None = None
        self.signals: list[str] = []
        self.side_commands: list[str] = []
        self.install_result = (0, b"", b"")
        self.install_timeout = False
        # Whether the image holds the Skill's dependency manifests
        # (/opt/manor/skill-deps); by default nothing is preinstalled.
        self.preinstalled_deps = False
        # KiB the container's disk-backed install dir holds (``du -skx``).
        self.local_disk_kb = 0
        # Dangling local-disk volumes ``docker volume ls`` reports.
        self.dangling_volumes: list[str] = []
        self.inject_fails = False
        # Baked Skills the container's image holds (name -> digest), and the
        # materialize calls received (root, name, digest, target, replace).
        self.baked_digests: dict[str, str] = {}
        self.materialized: list[list[str]] = []
        self.pid_available = True
        self.lock = threading.Lock()

    def __call__(self, args, *, input_data=None, timeout=120, allow_failure=False):
        with self.lock:
            self.calls.append(list(args))
        verb = args[1]
        if verb == "inspect":
            if not self.exists:
                return completed(args, 1, stderr=b"Error: No such object: skill-sbx-demo")
            return completed(args, 0, stdout=b"true\n" if self.running else b"false\n")
        if verb == "rm":
            if self.rm_fails:
                return completed(args, 1, stderr=b"device or resource busy")
            self.exists = False
            self.running = False
            self.killed.set()
            return completed(args)
        if verb == "ps":
            return completed(args, 0, stdout=b"")
        if verb == "volume":
            if args[2] == "ls":
                return completed(args, 0, stdout="".join(f"{v}\n" for v in self.dangling_volumes).encode())
            if args[2] == "rm":
                self.dangling_volumes.remove(args[3])
                return completed(args)
        if verb != "exec":
            raise AssertionError(f"unexpected docker call {args}")
        if not self.running:
            return completed(args, 1, stderr=NOT_RUNNING)
        joined = " ".join(args)
        if args[3:5] == ["python3", "-c"] and args[6:7] == ["/opt/manor/skills"]:
            # Baked Skill materialization: <root> <name> <digest> <target> <replace>.
            name, digest = args[7], args[8]
            self.materialized.append(args[6:])
            if self.baked_digests.get(name) != digest:
                return completed(args, 65)
            return completed(args)
        if args[3:5] == ["du", "-skx"]:
            return completed(args, 0, stdout=f"{self.local_disk_kb}\t{args[5]}\n".encode())
        if "start_new_session=True" in joined:
            return self._launcher(args)
        command = args[-1]
        if "/opt/manor/skill-deps/" in command:
            if self.preinstalled_deps:
                return completed(args)
            return completed(args, 1 if command.startswith("test -f") else 64)
        if "pip install" in command or "npm install" in command:
            if self.install_timeout:
                raise DockerCommandTimeout(timeout, b"Collecting x", b"")
            return completed(args, *self.install_result)
        if args[-4:-2] == ["-C", "/skill"] or "tar" in args:
            if self.inject_fails:
                return completed(args, 2, stderr=b"tar: write error")
            return completed(args)
        if command.startswith("cat /tmp/manor-exec"):
            if not self.pid_available:
                return completed(args, 1, stderr=b"cat: /tmp/manor-exec.pid: No such file or directory")
            return completed(args, 0, stdout=b"321")
        if command.startswith("kill -0"):
            return completed(args, 1)  # the group is gone once signalled
        if command.startswith("kill -"):
            self.signals.append(command)
            self.killed.set()
            return completed(args)
        if "pathlib" in joined:  # event mailbox reader
            return completed(args, 0, stdout=b"")
        with self.lock:
            self.side_commands.append(command)
        if command.startswith("head -c"):
            return completed(args, 0, stdout=b"file-content")
        if command.startswith(("stat -c", "stat -L -c")):
            return completed(args, 0, stdout=b"4")
        if command.startswith("base64"):
            return completed(args, 0, stdout=b"ZGF0YQ==")
        return completed(args)

    def _launcher(self, args):
        self.launcher_started.set()
        if self.launcher_timeout is not None:
            raise self.launcher_timeout
        if self.block:
            deadline = time.monotonic() + 10
            while not (self.release.is_set() or self.killed.is_set()):
                if time.monotonic() > deadline:
                    raise AssertionError("blocked launcher was never released")
                time.sleep(0.005)
        if self.killed.is_set():
            # A killed process group exits 143; a removed container 137.
            return completed(args, 137 if not self.exists else 143)
        return completed(args, *self.launcher_result)


class FakeLease:
    def __init__(self) -> None:
        self.released = 0

    async def release(self) -> None:
        self.released += 1

    async def renew(self) -> bool:
        return True


async def register_sandbox(runner, sandbox_id: str = "sbx-1", *, idle_for: float = 120.0):
    sandbox = DockerSandbox(sandbox_id, ContainerConfig())
    sandbox.container_name = f"skill-sbx-{sandbox_id}"
    sandbox.status = SandboxStatus.READY
    sandbox.created_at = time.time() - idle_for
    sandbox.last_used_at = time.time() - idle_for
    sandbox.skill = SkillManifest(
        name="demo", skill_dir="/old", scripts=["run.py"], entry_hint="run.py"
    )
    sandbox._skill_env = {"DEMO_KEY": "old"}
    runner._sandboxes[sandbox_id] = sandbox
    runner._fs_bridges[sandbox_id] = FsBridge(sandbox)
    assert await runner._local_active_capacity.try_acquire()
    # A registered sandbox holds its container's memory limit, exactly like
    # one the runner created; every reclaim path must hand it back.
    ticket = runner._memory.try_reserve(runner._memory_limit_bytes(sandbox.config))
    assert ticket is not None
    runner._memory_reservations[sandbox_id] = ticket
    lease = FakeLease()
    runner._active_leases[sandbox_id] = lease
    return sandbox, lease


def load_service_main(monkeypatch, name: str):
    spec = importlib.util.spec_from_file_location(name, SANDBOX_SERVICE_ROOT / "main.py")
    assert spec and spec.loader
    main = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, main)
    spec.loader.exec_module(main)
    monkeypatch.setattr(main.config, "API_TOKEN", API_TOKEN)
    return main


def describe_app_routes(app) -> str:
    """Diagnostics for an app whose /api/v1 routes went missing in a CI shard."""
    import fastapi

    routes_module = sys.modules.get("api.routes")
    router = getattr(routes_module, "router", None)
    lines = [
        f"app routes ({len(app.routes)}): "
        + ", ".join(
            f"{type(route).__name__}:{getattr(route, 'path', '?')}:{sorted(getattr(route, 'methods', None) or [])}"
            for route in app.routes
        )[:3000],
        f"api.routes file={getattr(routes_module, '__file__', None)} router_routes={len(getattr(router, 'routes', []) or [])}",
        f"api package={getattr(sys.modules.get('api'), '__file__', None)} path={getattr(sys.modules.get('api'), '__path__', None)}",
        f"fastapi {fastapi.__version__} at {fastapi.__file__}",
        f"sys.path[:6]={sys.path[:6]}",
    ]
    return "\n".join(lines)
