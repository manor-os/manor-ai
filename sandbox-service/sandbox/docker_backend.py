"""
Docker container backend for sandbox execution.

Manages the full lifecycle of a sandbox container:
  create → inject files → install deps → exec commands → destroy

Design mirrors OpenClaw's docker.ts / docker-backend.ts:
- Long-lived container (`sleep infinity`) reused across exec calls
- read-only root + tmpfs for workdir
- cap-drop ALL, no-new-privileges, network isolation
- Config hash tracking for stale container detection
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shlex
import subprocess
import time
import uuid
from typing import Optional

from sandbox_config import config as app_config

from .models import ContainerConfig, ExecResponse, SandboxStatus, SkillManifest

logger = logging.getLogger(__name__)

# Grace window: if a container was used within this window, don't auto-recreate
# on config mismatch (mirrors OpenClaw's HOT_CONTAINER_WINDOW_MS).
_HOT_WINDOW_SECONDS = 300
_CANCEL_GRACE_SECONDS = 1.0
_CANCEL_POLL_INTERVAL_SECONDS = 0.1
_EXECUTION_PID_WAIT_SECONDS = 0.5


def _run_docker(
    args: list[str],
    *,
    input_data: bytes | None = None,
    timeout: int = 120,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run a docker CLI command synchronously."""
    try:
        result = subprocess.run(
            args,
            input=input_data,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError(
            'Sandbox requires Docker, but the "docker" command was not found. '
            "Install Docker and ensure it is on PATH."
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Docker command timed out after {timeout}s: {args}") from exc

    if result.returncode != 0 and not allow_failure:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Docker command failed (exit {result.returncode}): {stderr}")
    return result


def _container_state(name: str) -> dict[str, bool]:
    result = _run_docker(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        allow_failure=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        normalized = " ".join(
            "".join(character if character.isprintable() else " " for character in stderr).split()
        )
        lowered = normalized.casefold()
        if "no such object" in lowered or "no such container" in lowered:
            return {"exists": False, "running": False}
        detail = normalized or "no error details"
        if len(detail) > 500:
            detail = f"{detail[:497]}..."
        raise RuntimeError(
            f"Docker inspect failed for container {name!r} "
            f"(exit {result.returncode}): {detail}"
        )
    running = result.stdout.decode().strip() == "true"
    return {"exists": True, "running": running}


def _image_exists_locally(image: str) -> bool:
    """Check the local Docker image cache without triggering a pull."""
    result = _run_docker(
        ["docker", "image", "inspect", image],
        allow_failure=True,
    )
    return result.returncode == 0


def _missing_image_message(image: str) -> str:
    return (
        f"Sandbox base image '{image}' is not available locally. "
        "Skill execution cannot start until the image exists on the Docker host. "
        f"Build it with: docker build -t {image} -f docker/Dockerfile.sandbox . "
        "Or set SANDBOX_IMAGE to an existing sandbox runtime image."
    )


def list_managed_sandbox_containers() -> list[str]:
    """List containers owned by this runner family using the Docker label."""
    result = _run_docker(
        ["docker", "ps", "-aq", "--filter", "label=sandbox.service=1"],
        allow_failure=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Failed to list managed sandbox containers: {stderr}")
    container_ids: list[str] = []
    for line in result.stdout.decode().splitlines():
        if container_id := line.strip():
            container_ids.append(container_id)
    return container_ids


def remove_managed_sandbox_container(container_id: str) -> None:
    """Force-remove one managed container discovered during reconciliation."""
    result = _run_docker(
        ["docker", "rm", "-f", container_id],
        allow_failure=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Failed to remove managed sandbox container: {stderr}")


def _config_hash(cfg: ContainerConfig, skill_dir: str) -> str:
    """Deterministic hash of container config + skill directory for staleness check."""
    payload = (
        f"{cfg.image}|{cfg.network}|{cfg.memory}|{cfg.cpus}|{cfg.pids_limit}"
        f"|{cfg.read_only_root}|{cfg.workdir}|{cfg.workdir_tmpfs_size}|{skill_dir}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


class DockerSandbox:
    """
    Manages a single Docker container for skill execution.

    Thread-safe for concurrent lifecycle transitions against the same container.
    """

    def __init__(self, sandbox_id: str, config: ContainerConfig):
        self.sandbox_id = sandbox_id
        self.config = config
        self.container_name: str = ""
        self.status: SandboxStatus = SandboxStatus.CREATING
        self.skill: Optional[SkillManifest] = None
        self.created_at: float = 0.0
        self.last_used_at: float = 0.0
        self._config_hash: str = ""
        self._state_lock = asyncio.Lock()
        self._execution_state_lock = asyncio.Lock()
        self._active_command: str | None = None
        self._active_execution_id: str | None = None
        self._exec_started_at: float | None = None
        self._destroy_requested: bool = False

    @property
    def active_command(self) -> str | None:
        return self._active_command

    @property
    def active_execution_id(self) -> str | None:
        return self._active_execution_id

    @property
    def expires_at(self) -> float | None:
        if self.status in (SandboxStatus.DESTROYED, SandboxStatus.DESTROYING):
            return None
        return (
            self.last_used_at + app_config.IDLE_TIMEOUT_SECONDS
            if self.last_used_at
            else None
        )

    def touch(self) -> None:
        """Refresh the idle lease without executing untrusted project code."""

        if self.status in (SandboxStatus.DESTROYED, SandboxStatus.DESTROYING):
            raise RuntimeError(f"Sandbox is not running (status={self.status.value})")
        self.last_used_at = time.time()

    def is_active(self) -> bool:
        return self.status in (SandboxStatus.CREATING, SandboxStatus.INSTALLING, SandboxStatus.EXECUTING, SandboxStatus.DESTROYING)

    def ensure_available(self) -> None:
        """Reject operations once the sandbox is quarantined or being removed."""
        if self._destroy_requested or self.status in (
            SandboxStatus.ERROR,
            SandboxStatus.DESTROYING,
            SandboxStatus.DESTROYED,
        ):
            raise RuntimeError(f"Sandbox unavailable (status={self.status.value})")

    # ── lifecycle ──

    async def setup(
        self,
        skill: SkillManifest,
        env: dict[str, str],
        auto_install: bool = True,
    ) -> None:
        """Full setup: create container, inject files, optionally install deps."""
        async with self._state_lock:
            self.skill = skill
            self.created_at = time.time()
            self.last_used_at = self.created_at

            name = self._resolve_container_name(skill)
            self.container_name = name
            self._config_hash = _config_hash(self.config, skill.skill_dir)

            await self._ensure_container(skill, env)

            self.status = SandboxStatus.RUNNING
            await self._inject_files(skill)

            if auto_install and (skill.requirements_txt or skill.package_json):
                self.status = SandboxStatus.INSTALLING
                result = await self._install_dependencies(skill)
                if result.exit_code != 0:
                    self.status = SandboxStatus.ERROR
                    raise RuntimeError(
                        f"dependency install failed (exit {result.exit_code}):\n{result.stderr}"
                    )

            self.last_used_at = time.time()
            self.status = SandboxStatus.READY

    async def exec(
        self,
        command: str,
        timeout: int | None = None,
        workdir: str | None = None,
        execution_id: str | None = None,
    ) -> ExecResponse:
        """Execute a command inside the running container."""
        self.ensure_available()
        if self.status in (SandboxStatus.DESTROYED, SandboxStatus.CREATING, SandboxStatus.DESTROYING):
            raise RuntimeError(f"Sandbox is not running (status={self.status.value})")

        timeout = timeout or self.config.exec_timeout
        wd = workdir or self.config.workdir
        resolved_execution_id = execution_id or uuid.uuid4().hex

        async with self._state_lock:
            self.ensure_available()
            if self.status in (SandboxStatus.DESTROYED, SandboxStatus.DESTROYING, SandboxStatus.CREATING):
                raise RuntimeError(f"Sandbox is not running (status={self.status.value})")
            if self.status == SandboxStatus.INSTALLING:
                raise RuntimeError("Sandbox is installing dependencies — please wait and retry.")
            if self.status == SandboxStatus.EXECUTING:
                raise RuntimeError("Sandbox is already executing a command — please wait and retry.")
            result = await self._exec_in_container(
                command,
                timeout=timeout,
                workdir=wd,
                mark_status=SandboxStatus.EXECUTING,
                execution_id=resolved_execution_id,
            )

        return result

    async def _exec_in_container(
        self,
        command: str,
        *,
        timeout: int,
        workdir: str,
        mark_status: SandboxStatus,
        execution_id: str | None = None,
    ) -> ExecResponse:
        """Internal docker exec helper. Caller must already hold _state_lock."""
        self.status = mark_status
        self._active_command = command[:500]
        self._exec_started_at = time.time()
        self.last_used_at = self._exec_started_at

        pid_file: str | None = None
        if execution_id is not None:
            pid_file = self._execution_pid_file(execution_id)
            await self._set_active_execution(execution_id)
            args = self._execution_command_args(command, workdir, pid_file)
        else:
            args = [
                "docker", "exec", "-i",
                "-w", workdir,
                self.container_name,
                "sh", "-c", command,
            ]

        docker_task = asyncio.create_task(
            asyncio.to_thread(
                _run_docker,
                args,
                timeout=timeout,
                allow_failure=True,
            )
        )
        try:
            result = await asyncio.shield(docker_task)
        except BaseException as exc:
            await self._wait_for_task_terminal(docker_task)
            if (
                execution_id is not None
                and isinstance(exc, RuntimeError)
                and "Docker command timed out" in str(exc)
            ):
                try:
                    await self.cancel_execution(execution_id)
                except Exception:
                    logger.exception(
                        "Failed to terminate timed-out sandbox execution %s",
                        execution_id,
                    )
            raise
        finally:
            if execution_id is not None:
                await self._clear_active_execution(execution_id)
            if pid_file is not None:
                await self._remove_execution_pid_file(pid_file)
            self._active_command = None
            self._exec_started_at = None
            self.last_used_at = time.time()
            if self.status not in (SandboxStatus.DESTROYED, SandboxStatus.ERROR):
                self.status = SandboxStatus.READY

        return ExecResponse(
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
            exit_code=result.returncode,
            execution_id=execution_id,
        )

    async def cancel_execution(self, execution_id: str) -> bool:
        """Terminate the exact active execution without waiting for its lifecycle lock."""
        if not await self._is_active_execution(execution_id):
            return False

        pid_file = self._execution_pid_file(execution_id)
        pid = await self._wait_for_execution_pid(execution_id, pid_file)
        if pid is None:
            if not await self._is_active_execution(execution_id):
                return False
            raise RuntimeError(f"Execution process was not ready for cancellation: {execution_id}")

        if not await self._signal_execution_group(pid, "TERM"):
            return False
        deadline = asyncio.get_running_loop().time() + _CANCEL_GRACE_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if not await self._is_active_execution(execution_id):
                return True
            if not await self._execution_group_alive(pid):
                return True
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_SECONDS)

        if (
            await self._is_active_execution(execution_id)
            and await self._execution_group_alive(pid)
        ):
            await self._signal_execution_group(pid, "KILL")
        return True

    def _execution_command_args(
        self,
        command: str,
        workdir: str,
        pid_file: str,
    ) -> list[str]:
        launcher = (
            "import os,subprocess,sys;"
            "proc=subprocess.Popen(['sh','-c',sys.argv[1]],start_new_session=True);"
            "fd=os.open(sys.argv[2],os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);"
            "os.write(fd,str(proc.pid).encode('ascii'));"
            "os.close(fd);"
            "code=proc.wait();"
            "raise SystemExit(code if code>=0 else 128-code)"
        )
        return [
            "docker", "exec", "-i",
            "-w", workdir,
            self.container_name,
            "python", "-c", launcher, command, pid_file,
        ]

    @staticmethod
    def _execution_pid_file(execution_id: str) -> str:
        safe_id = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:24]
        return f"/tmp/manor-exec-{safe_id}.pid"

    async def _set_active_execution(self, execution_id: str) -> None:
        async with self._execution_state_lock:
            self._active_execution_id = execution_id

    async def _clear_active_execution(self, execution_id: str) -> None:
        async with self._execution_state_lock:
            if self._active_execution_id == execution_id:
                self._active_execution_id = None

    async def _is_active_execution(self, execution_id: str) -> bool:
        async with self._execution_state_lock:
            return self._active_execution_id == execution_id

    async def _wait_for_execution_pid(
        self,
        execution_id: str,
        pid_file: str,
    ) -> int | None:
        deadline = asyncio.get_running_loop().time() + _EXECUTION_PID_WAIT_SECONDS
        while True:
            if not await self._is_active_execution(execution_id):
                return None
            result = await asyncio.to_thread(
                _run_docker,
                ["docker", "exec", self.container_name, "sh", "-c", f"cat {shlex.quote(pid_file)}"],
                timeout=2,
                allow_failure=True,
            )
            if result.returncode == 0:
                raw_pid = result.stdout.decode("ascii", errors="ignore").strip()
                if raw_pid.isdigit() and int(raw_pid) > 1:
                    return int(raw_pid)
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_SECONDS)

    async def _signal_execution_group(self, pid: int, signal: str) -> bool:
        result = await asyncio.to_thread(
            _run_docker,
            [
                "docker", "exec", self.container_name, "sh", "-c",
                f"kill -{signal} -{pid}",
            ],
            timeout=5,
            allow_failure=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            if "no such process" in stderr.casefold() or not stderr:
                return False
            raise RuntimeError(f"Failed to send {signal} to execution process group: {stderr}")
        return True

    async def _execution_group_alive(self, pid: int) -> bool:
        result = await asyncio.to_thread(
            _run_docker,
            ["docker", "exec", self.container_name, "sh", "-c", f"kill -0 -{pid}"],
            timeout=5,
            allow_failure=True,
        )
        if result.returncode == 0:
            return True
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if "no such process" in stderr.casefold() or not stderr:
            return False
        raise RuntimeError(f"Failed to inspect execution process group: {stderr}")

    async def _remove_execution_pid_file(self, pid_file: str) -> None:
        try:
            await asyncio.to_thread(
                _run_docker,
                ["docker", "exec", self.container_name, "sh", "-c", f"rm -f {shlex.quote(pid_file)}"],
                timeout=5,
                allow_failure=True,
            )
        except Exception:
            logger.debug("Failed to remove execution pid file", exc_info=True)

    @staticmethod
    async def _wait_for_task_terminal(task: asyncio.Task[object]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                pass

    async def destroy(self) -> None:
        """Force-remove the container after any active lifecycle operation completes."""
        self._destroy_requested = True
        async with self._state_lock:
            self.status = SandboxStatus.DESTROYING
            try:
                if self.container_name:
                    result = await asyncio.to_thread(
                        _run_docker,
                        ["docker", "rm", "-f", self.container_name],
                        allow_failure=True,
                    )
                    if result.returncode != 0:
                        state = await asyncio.to_thread(_container_state, self.container_name)
                        if state["exists"]:
                            stderr = result.stderr.decode("utf-8", errors="replace").strip()
                            raise RuntimeError(
                                f"Failed to destroy sandbox container {self.container_name}: "
                                f"docker rm exited {result.returncode}, but the container still exists"
                                + (f": {stderr}" if stderr else "")
                            )
            except BaseException:
                self.status = SandboxStatus.ERROR
                raise
            self._active_command = None
            self._exec_started_at = None
            self.status = SandboxStatus.DESTROYED

    # ── internals ──

    def _resolve_container_name(self, skill: SkillManifest) -> str:
        normalized_skill_name = "".join(
            char.lower() if char.isalnum() else "-" for char in skill.name
        ).strip("-") or "skill"
        scope = hashlib.sha256(
            f"{self.sandbox_id}:{normalized_skill_name}".encode()
        ).hexdigest()[:10]
        raw = f"{self.config.container_prefix}{normalized_skill_name}-{scope}"
        # Docker container names: [a-zA-Z0-9][a-zA-Z0-9_.-], max 63 chars
        safe = "".join(c if c.isalnum() or c in "_.-" else "-" for c in raw)
        return safe[:63]

    async def _ensure_container(
        self, skill: SkillManifest, env: dict[str, str],
    ) -> None:
        """Create (or reuse) the Docker container."""
        state = await asyncio.to_thread(_container_state, self.container_name)

        if state["exists"] and state["running"]:
            logger.info("Reusing running container %s", self.container_name)
            return

        if state["exists"]:
            await asyncio.to_thread(
                _run_docker,
                ["docker", "rm", "-f", self.container_name],
                allow_failure=True,
            )

        if not await asyncio.to_thread(_image_exists_locally, self.config.image):
            raise RuntimeError(_missing_image_message(self.config.image))

        create_args = self._build_create_args(env)
        await asyncio.to_thread(_run_docker, create_args)
        await asyncio.to_thread(
            _run_docker, ["docker", "start", self.container_name],
        )
        logger.info("Created and started container %s", self.container_name)

    def _build_create_args(self, env: dict[str, str]) -> list[str]:
        cfg = self.config
        args = ["docker", "create", "--name", self.container_name]

        # Security hardening
        if cfg.read_only_root:
            args.append("--read-only")
        for t in cfg.tmpfs:
            args.extend(["--tmpfs", t])
        # Skill workdir as tmpfs so writes work with read-only root.
        # mode=1777 ensures any user (including non-root sandbox user) can write.
        args.extend(
            [
                "--tmpfs",
                f"{cfg.workdir}:exec,size={cfg.workdir_tmpfs_size},mode=1777",
            ]
        )
        # pip cache needs /root writable (root user installs).
        args.extend(["--tmpfs", "/root:exec,size=128m"])
        # npm / sandbox user home: npm cache, .npm config, node_modules etc.
        args.extend(["--tmpfs", "/home/sandbox:exec,size=512m,uid=1000,gid=1000,mode=0755"])

        args.extend(["--network", cfg.network])
        if cfg.network != "none":
            for dns_server in cfg.dns:
                args.extend(["--dns", dns_server])
        for cap in cfg.cap_drop:
            args.extend(["--cap-drop", cap])
        args.extend(["--security-opt", "no-new-privileges"])

        if cfg.memory:
            args.extend(["--memory", cfg.memory])
        if cfg.cpus > 0:
            args.extend(["--cpus", str(cfg.cpus)])
        if cfg.pids_limit > 0:
            args.extend(["--pids-limit", str(cfg.pids_limit)])

        for key, val in env.items():
            args.extend(["--env", f"{key}={val}"])

        for volume in getattr(cfg, "volumes", []) or []:
            args.extend(["--volume", volume])

        args.extend(["--label", "sandbox.service=1"])
        args.extend(["--label", f"sandbox.id={self.sandbox_id}"])
        if self._config_hash:
            args.extend(["--label", f"sandbox.configHash={self._config_hash}"])

        args.extend(["--workdir", cfg.workdir])
        args.extend([cfg.image, "sleep", "infinity"])
        return args

    async def _inject_files(
        self,
        skill: SkillManifest,
        *,
        clear_existing: bool = True,
    ) -> None:
        """
        Copy skill directory contents into the container.

        Clear the existing workdir when a different skill is loaded so sandbox
        reuse does not leak files across skills.  A same-skill reload overlays
        the packaged files instead, preserving session-owned directories such
        as ``/skill/projects`` so a later chat turn can continue the project.
        """

        def _tar_inject() -> None:
            import os
            entries = os.listdir(skill.skill_dir)
            if clear_existing:
                _run_docker(
                    [
                        "docker", "exec",
                        self.container_name,
                        "sh", "-c",
                        f"find {shlex.quote(self.config.workdir)} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +",
                    ],
                    allow_failure=True,
                )
            if not entries:
                return
            tar_create = subprocess.Popen(
                ["tar", "cf", "-", "-C", skill.skill_dir] + entries,
                stdout=subprocess.PIPE,
            )
            tar_extract = subprocess.run(
                [
                    "docker", "exec", "-i",
                    self.container_name,
                    "tar", "xf", "-", "-C", self.config.workdir,
                    "--no-same-owner", "--no-same-permissions",
                ],
                stdin=tar_create.stdout,
                capture_output=True,
                timeout=60,
            )
            tar_create.stdout.close()
            tar_create.wait()
            if tar_extract.returncode != 0:
                stderr = tar_extract.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"File injection failed (exit {tar_extract.returncode}): {stderr}")

            _run_docker(
                [
                    "docker", "exec",
                    self.container_name,
                    "sh", "-c",
                    f"find {self.config.workdir} -type f"
                    r" \( -name '*.sh' -o -name '*.bash' -o -name '*.py' \)"
                    r" -exec chmod +x {} +",
                ],
                allow_failure=True,
            )

        await asyncio.to_thread(_tar_inject)
        logger.info("Injected skill files into %s:%s", self.container_name, self.config.workdir)

    def is_idle(self, threshold: int = 30) -> bool:
        """Return True if the sandbox is ready and has been inactive for at least `threshold` seconds."""
        if self.status != SandboxStatus.READY:
            return False
        return (time.time() - self.last_used_at) > threshold

    async def load_skill(
        self,
        skill_name: str,
        files: dict[str, str],
        auto_install: bool = True,
        idle_threshold: int = 30,
    ) -> "SkillManifest":
        """
        Load a new skill into this sandbox (reuse mode).

        Copies the new skill's files into the running container and optionally
        re-installs Python dependencies. Rejects the call if the sandbox is
        not truly idle.
        """
        import shutil
        import tempfile
        from pathlib import Path

        async with self._state_lock:
            self.ensure_available()
            if self.status in (SandboxStatus.DESTROYED, SandboxStatus.CREATING, SandboxStatus.DESTROYING):
                raise RuntimeError(f"Sandbox unavailable (status={self.status.value})")
            if self.status in (SandboxStatus.INSTALLING, SandboxStatus.EXECUTING):
                raise RuntimeError(f"Sandbox is busy (status={self.status.value}) — please wait and retry.")
            if not self.is_idle(idle_threshold):
                idle_secs = time.time() - self.last_used_at
                raise RuntimeError(
                    f"Sandbox is busy (last active {idle_secs:.0f}s ago). "
                    f"Complete the current task or wait {max(0, idle_threshold - idle_secs):.0f}s."
                )

            tmp_dir = tempfile.mkdtemp(prefix=f"sbx-load-{skill_name}-")
            try:
                for rel_path, content in files.items():
                    safe_rel = rel_path.replace("\\", "/").strip().lstrip("/")
                    if not safe_rel or ".." in safe_rel:
                        continue
                    full_path = Path(tmp_dir) / safe_rel
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(content, encoding="utf-8")

                from .scanner import SkillScanner
                new_skill = SkillScanner().scan(tmp_dir)
                new_skill = new_skill.model_copy(update={"name": skill_name, "skill_dir": tmp_dir})

                same_skill = self.skill is not None and self.skill.name == skill_name
                self.status = SandboxStatus.RUNNING
                await self._inject_files(
                    new_skill,
                    clear_existing=not same_skill,
                )

                if auto_install and (new_skill.requirements_txt or new_skill.package_json):
                    self.status = SandboxStatus.INSTALLING
                    result = await self._install_dependencies(new_skill)
                    if result.exit_code != 0:
                        self.status = SandboxStatus.ERROR
                        raise RuntimeError(
                            f"dependency install failed (exit {result.exit_code}):\n{result.stderr}"
                        )

                self.skill = new_skill
                self.last_used_at = time.time()
                self.status = SandboxStatus.READY
                logger.info("Loaded skill '%s' into sandbox %s", skill_name, self.sandbox_id)
                return new_skill
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _install_dependencies(self, skill: SkillManifest) -> ExecResponse:
        """Run pip install and/or npm install inside the container."""
        needs_pip = bool(skill.requirements_txt)
        needs_npm = bool(skill.package_json)

        if not needs_pip and not needs_npm:
            return ExecResponse(stdout="", stderr="", exit_code=0)

        combined_stdout: list[str] = []
        combined_stderr: list[str] = []
        last_exit_code = 0

        if needs_pip:
            req_path = f"{self.config.workdir}/{skill.requirements_txt}"
            logger.info("install_dependencies: pip install from %s", req_path)
            result = await self._exec_in_container(
                f"pip install --no-cache-dir --root-user-action=ignore -r {req_path}",
                timeout=self.config.install_timeout,
                workdir=self.config.workdir,
                mark_status=SandboxStatus.INSTALLING,
            )
            if result.stdout:
                combined_stdout.append(result.stdout)
            if result.stderr:
                combined_stderr.append(result.stderr)
            last_exit_code = result.exit_code
            logger.info(
                "install_dependencies: pip install exit_code=%d", result.exit_code,
            )
            if result.exit_code != 0:
                return ExecResponse(
                    stdout="\n".join(combined_stdout),
                    stderr="\n".join(combined_stderr),
                    exit_code=result.exit_code,
                )

        if needs_npm:
            pkg_dir = f"{self.config.workdir}/{skill.package_json}".rsplit("/", 1)[0]
            logger.info(
                "install_dependencies: npm install in %s", pkg_dir,
            )
            result = await self._exec_in_container(
                "npm install --no-fund --no-audit",
                timeout=self.config.install_timeout,
                workdir=pkg_dir,
                mark_status=SandboxStatus.INSTALLING,
            )
            if result.stdout:
                combined_stdout.append(result.stdout)
            if result.stderr:
                combined_stderr.append(result.stderr)
            last_exit_code = result.exit_code
            logger.info(
                "install_dependencies: npm install exit_code=%d", result.exit_code,
            )

        return ExecResponse(
            stdout="\n".join(combined_stdout),
            stderr="\n".join(combined_stderr),
            exit_code=last_exit_code,
        )
