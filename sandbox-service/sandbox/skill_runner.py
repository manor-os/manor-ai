"""
Skill runner — the top-level orchestrator.

Manages a registry of active sandboxes and provides the high-level API
consumed by the FastAPI routes:
  - create sandbox for a skill
  - exec commands
  - read/write files
  - build LLM context
  - full run (create → exec → collect)
  - destroy / prune idle sandboxes
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from config import config as app_config

from .docker_backend import DockerSandbox, _image_exists_locally
from .fs_bridge import FsBridge
from .models import (
    ContainerConfig,
    CreateSandboxResponse,
    ExecResponse,
    FileReadBase64Response,
    FileReadResponse,
    FileWriteResponse,
    LoadSkillResponse,
    SandboxInfo,
    SkillContextResponse,
    SkillManifest,
    SkillRunResponse,
    SkillRunStepResult,
)
from .scanner import SkillScanner
from .security import (
    SecurityError,
    sanitize_env_vars,
    validate_container_path,
    validate_host_path,
)

logger = logging.getLogger(__name__)


class SkillRunner:
    """
    Central sandbox registry and orchestrator.

    Keeps a dict of active sandboxes keyed by sandbox_id.
    Provides async methods for the full sandbox lifecycle.
    """

    def __init__(self) -> None:
        self._sandboxes: dict[str, DockerSandbox] = {}
        self._fs_bridges: dict[str, FsBridge] = {}
        self._scanner = SkillScanner()
        self._prune_task: asyncio.Task | None = None

    # ── lifecycle hooks ──

    async def startup(self) -> None:
        """Called on app startup — begins background prune loop."""
        self._prune_task = asyncio.create_task(self._prune_loop())
        logger.info("SkillRunner started (max sandboxes=%d)", app_config.MAX_SANDBOXES)

    async def shutdown(self) -> None:
        """Called on app shutdown — destroy all sandboxes."""
        if self._prune_task:
            self._prune_task.cancel()
        tasks = [sbx.destroy() for sbx in self._sandboxes.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sandboxes.clear()
        self._fs_bridges.clear()
        logger.info("SkillRunner shut down, all sandboxes destroyed")

    # ── public API ──

    def scan_skill(self, skill_dir: str) -> SkillManifest:
        """Scan a skill directory and return its manifest."""
        return self._scanner.scan(skill_dir)

    def runtime_status(self) -> dict[str, object]:
        """Return sandbox runtime readiness without creating a container."""

        image = app_config.SANDBOX_IMAGE
        status: dict[str, object] = {"sandbox_image": image}
        try:
            status["sandbox_image_available"] = _image_exists_locally(image)
        except Exception as exc:
            status["sandbox_image_available"] = False
            status["sandbox_image_error"] = str(exc)
        return status

    async def create_sandbox(
        self,
        skill_dir: str,
        env: dict[str, str],
        allowed_sensitive_keys: set[str] | None = None,
        config_overrides: ContainerConfig | None = None,
        auto_install: bool = True,
    ) -> CreateSandboxResponse:
        """Create a sandbox for a skill, inject files, install deps."""
        # Validate the host path
        validate_host_path(skill_dir)

        # Scan skill
        skill = self._scanner.scan(skill_dir)

        # Enforce sandbox limit
        if len(self._sandboxes) >= app_config.MAX_SANDBOXES:
            await self._prune_idle()
            if len(self._sandboxes) >= app_config.MAX_SANDBOXES:
                raise RuntimeError(
                    f"Max sandbox limit reached ({app_config.MAX_SANDBOXES}). "
                    "Destroy idle sandboxes first."
                )

        # Sanitize env vars
        safe_env, blocked = sanitize_env_vars(env, allowed_sensitive_keys)

        # Resolve config. Overrides are partial by design: the API caller can
        # add bind mounts without replacing service-owned defaults such as
        # image, memory, workdir, or timeouts.
        cfg = self._resolve_config(config_overrides)

        # Create sandbox
        sandbox_id = str(uuid.uuid4())[:12]
        sandbox = DockerSandbox(sandbox_id, cfg)

        try:
            await sandbox.setup(skill, safe_env, auto_install=auto_install)
        except Exception:
            await sandbox.destroy()
            raise

        self._sandboxes[sandbox_id] = sandbox
        self._fs_bridges[sandbox_id] = FsBridge(sandbox)

        return CreateSandboxResponse(
            sandbox_id=sandbox_id,
            container_name=sandbox.container_name,
            status=sandbox.status,
            skill=skill,
            workdir=cfg.workdir,
            env_blocked=blocked,
        )

    async def create_sandbox_from_files(
        self,
        skill_name: str,
        files: dict[str, str],
        env: dict[str, str],
        allowed_sensitive_keys: set[str] | None = None,
        config_overrides: ContainerConfig | None = None,
        auto_install: bool = True,
    ) -> CreateSandboxResponse:
        """
        Create a sandbox from in-memory file contents (no host path required).

        Used when skill files live in remote storage (e.g. MinIO) and are not
        available on the sandbox host filesystem. Writes files to a temp
        directory, scans, creates the sandbox, then cleans up the temp dir.
        """
        import shutil
        import tempfile
        from pathlib import Path

        if not files:
            raise ValueError("files dict must not be empty")

        tmp_dir = tempfile.mkdtemp(prefix=f"sbx-{skill_name}-")
        try:
            for rel_path, content in files.items():
                safe_rel = rel_path.replace("\\", "/").strip().lstrip("/")
                if not safe_rel or ".." in safe_rel:
                    continue
                full_path = Path(tmp_dir) / safe_rel
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")

            return await self.create_sandbox(
                skill_dir=tmp_dir,
                env=env,
                allowed_sensitive_keys=allowed_sensitive_keys,
                config_overrides=config_overrides,
                auto_install=auto_install,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def exec_command(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: str | None = None,
    ) -> ExecResponse:
        """Execute a command in an existing sandbox."""
        sandbox = self._get_sandbox(sandbox_id)
        return await sandbox.exec(command, timeout=timeout, workdir=workdir)

    async def read_file(
        self,
        sandbox_id: str,
        path: str,
        max_size: int = 65536,
    ) -> FileReadResponse:
        """Read a file from a sandbox."""
        bridge = self._get_bridge(sandbox_id)
        content, truncated = await bridge.read_file(path, max_size=max_size)
        return FileReadResponse(
            path=path,
            content=content,
            size=len(content),
            truncated=truncated,
        )

    async def read_file_base64(
        self,
        sandbox_id: str,
        path: str,
        max_size: int = 50 * 1024 * 1024,
    ) -> FileReadBase64Response:
        """Read a binary file from a sandbox as base64."""
        bridge = self._get_bridge(sandbox_id)
        content_b64, file_size = await bridge.read_file_base64(path, max_size=max_size)
        return FileReadBase64Response(
            path=path,
            content_base64=content_b64,
            size=file_size,
        )

    async def write_file(
        self,
        sandbox_id: str,
        path: str,
        content: str,
        mkdir: bool = True,
    ) -> FileWriteResponse:
        """Write a file into a sandbox."""
        bridge = self._get_bridge(sandbox_id)
        await bridge.write_file(path, content, mkdir=mkdir)
        return FileWriteResponse(path=path, written=True)

    async def write_file_base64(
        self,
        sandbox_id: str,
        path: str,
        content_base64: str,
        mkdir: bool = True,
    ) -> FileWriteResponse:
        """Write a binary file into a sandbox from base64 content."""
        bridge = self._get_bridge(sandbox_id)
        await bridge.write_file_base64(path, content_base64, mkdir=mkdir)
        return FileWriteResponse(path=path, written=True)

    async def get_skill_context(
        self,
        sandbox_id: str,
        max_files: int = 5,
        max_file_size: int = 8000,
    ) -> SkillContextResponse:
        """
        Build structured context for an LLM to understand the skill.
        Returns the skill manifest + key file contents + sandbox info.
        """
        sandbox = self._get_sandbox(sandbox_id)
        bridge = self._get_bridge(sandbox_id)
        skill = sandbox.skill
        if not skill:
            raise RuntimeError("Sandbox has no skill attached")

        file_contents = await bridge.read_key_skill_files(
            skill.scripts,
            entry_hint=skill.entry_hint,
            max_files=max_files,
            max_size=max_file_size,
        )

        sandbox_info = (
            f"Working directory: {sandbox.config.workdir}\n"
            f"Network: {sandbox.config.network}\n"
            f"Memory limit: {sandbox.config.memory}\n"
            f"Image: {sandbox.config.image}\n"
            f"Python available: yes\n"
        )
        if skill.requirements_txt:
            sandbox_info += "Dependencies: installed from requirements.txt\n"

        return SkillContextResponse(
            sandbox_id=sandbox_id,
            skill=skill,
            file_contents=file_contents,
            sandbox_info=sandbox_info,
        )

    async def run_skill(
        self,
        skill_dir: str,
        env: dict[str, str],
        commands: list[str],
        allowed_sensitive_keys: set[str] | None = None,
        config_overrides: ContainerConfig | None = None,
        auto_destroy: bool = True,
    ) -> SkillRunResponse:
        """
        Full end-to-end: create sandbox → exec commands → return results.

        If ``commands`` is empty, auto-detect entry point from skill manifest.
        """
        resp = await self.create_sandbox(
            skill_dir=skill_dir,
            env=env,
            allowed_sensitive_keys=allowed_sensitive_keys,
            config_overrides=config_overrides,
        )
        sandbox_id = resp.sandbox_id
        skill = resp.skill

        # Auto-detect commands if none provided
        if not commands:
            commands = self._auto_detect_commands(skill)

        steps: list[SkillRunStepResult] = []
        all_ok = True

        for cmd in commands:
            result = await self.exec_command(sandbox_id, cmd)
            steps.append(SkillRunStepResult(
                command=cmd,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
            ))
            if result.exit_code != 0:
                all_ok = False
                break

        destroyed = False
        if auto_destroy:
            await self.destroy_sandbox(sandbox_id)
            destroyed = True

        return SkillRunResponse(
            sandbox_id=sandbox_id,
            skill=skill,
            success=all_ok,
            steps=steps,
            destroyed=destroyed,
        )

    async def load_skill_into_sandbox(
        self,
        sandbox_id: str,
        skill_name: str,
        files: dict[str, str],
        auto_install: bool = True,
        idle_threshold: int = 30,
    ) -> LoadSkillResponse:
        """
        Reload a sandbox with a new skill's files (reuse mode).

        The sandbox container keeps running; only the skill files are replaced.
        Raises RuntimeError if the sandbox is busy or unavailable.
        """
        sandbox = self._get_sandbox(sandbox_id)
        new_skill = await sandbox.load_skill(
            skill_name=skill_name,
            files=files,
            auto_install=auto_install,
            idle_threshold=idle_threshold,
        )
        return LoadSkillResponse(sandbox_id=sandbox_id, skill=new_skill, reused=True)

    async def destroy_sandbox(self, sandbox_id: str) -> None:
        """Destroy a sandbox and remove from registry."""
        sandbox = self._sandboxes.pop(sandbox_id, None)
        self._fs_bridges.pop(sandbox_id, None)
        if sandbox:
            await sandbox.destroy()

    def list_sandboxes(self) -> list[SandboxInfo]:
        """List all active sandboxes."""
        result: list[SandboxInfo] = []
        for sid, sbx in self._sandboxes.items():
            result.append(SandboxInfo(
                sandbox_id=sid,
                container_name=sbx.container_name,
                status=sbx.status,
                skill_name=sbx.skill.name if sbx.skill else "unknown",
                workdir=sbx.config.workdir,
                created_at=sbx.created_at,
                last_used_at=sbx.last_used_at,
                config=sbx.config,
                active_command=sbx.active_command,
                expires_at=sbx.expires_at,
            ))
        return result

    def get_sandbox_status(self, sandbox_id: str) -> SandboxInfo:
        sbx = self._get_sandbox(sandbox_id)
        return SandboxInfo(
            sandbox_id=sandbox_id,
            container_name=sbx.container_name,
            status=sbx.status,
            skill_name=sbx.skill.name if sbx.skill else "unknown",
            workdir=sbx.config.workdir,
            created_at=sbx.created_at,
            last_used_at=sbx.last_used_at,
            config=sbx.config,
            active_command=sbx.active_command,
            expires_at=sbx.expires_at,
        )

    # ── internal helpers ──

    def _get_sandbox(self, sandbox_id: str) -> DockerSandbox:
        sandbox = self._sandboxes.get(sandbox_id)
        if not sandbox:
            raise KeyError(f"Sandbox not found: {sandbox_id}")
        return sandbox

    def _get_bridge(self, sandbox_id: str) -> FsBridge:
        bridge = self._fs_bridges.get(sandbox_id)
        if not bridge:
            raise KeyError(f"Sandbox not found: {sandbox_id}")
        return bridge

    def _resolve_config(self, overrides: ContainerConfig | None = None) -> ContainerConfig:
        cfg = self._default_config()
        if overrides:
            fields = getattr(overrides, "model_fields_set", None)
            if not fields:
                fields = set(type(overrides).model_fields.keys())
            values = overrides.model_dump()
            for field in fields:
                if field in values:
                    setattr(cfg, field, values[field])
        self._validate_config(cfg)
        return cfg

    @staticmethod
    def _validate_config(cfg: ContainerConfig) -> None:
        for volume in cfg.volumes or []:
            parts = volume.split(":")
            if len(parts) not in (2, 3):
                raise SecurityError(
                    "Volume mounts must use 'host_path:container_path[:mode]' syntax."
                )
            host_path, container_path = parts[0], parts[1]
            mode = parts[2] if len(parts) == 3 else ""
            validate_host_path(host_path)
            validate_container_path(container_path)
            if mode and mode not in {"ro", "rw"}:
                raise SecurityError(
                    f"Unsupported volume mode '{mode}'. Use 'ro' or 'rw'."
                )

    def _default_config(self) -> ContainerConfig:
        return ContainerConfig(
            image=app_config.SANDBOX_IMAGE,
            network=app_config.SANDBOX_NETWORK,
            dns=app_config.SANDBOX_DNS_SERVERS,
            memory=app_config.SANDBOX_MEMORY,
            cpus=app_config.SANDBOX_CPUS,
            pids_limit=app_config.SANDBOX_PIDS_LIMIT,
            read_only_root=app_config.SANDBOX_READ_ONLY_ROOT,
            container_prefix=app_config.SANDBOX_CONTAINER_PREFIX,
            workdir=app_config.SANDBOX_WORKDIR,
            install_timeout=app_config.INSTALL_TIMEOUT,
            exec_timeout=app_config.EXEC_TIMEOUT,
        )

    @staticmethod
    def _auto_detect_commands(skill: SkillManifest) -> list[str]:
        """Infer which command(s) to run based on the skill manifest."""
        if skill.entry_hint:
            ext = skill.entry_hint.rsplit(".", 1)[-1] if "." in skill.entry_hint else ""
            if ext == "py":
                return [f"python {skill.entry_hint}"]
            if ext in ("sh", "bash"):
                return [f"bash {skill.entry_hint}"]
            if ext in ("js", "ts"):
                return [f"node {skill.entry_hint}"]
            return [f"./{skill.entry_hint}"]

        # Fallback: try common patterns
        for script in skill.scripts:
            if script.endswith(".py"):
                return [f"python {script}"]
        return []

    async def _prune_idle(self) -> None:
        """Remove sandboxes that have been idle beyond the threshold."""
        now = time.time()
        to_remove: list[str] = []
        for sid, sbx in self._sandboxes.items():
            idle = now - sbx.last_used_at
            if idle > app_config.IDLE_TIMEOUT_SECONDS:
                to_remove.append(sid)
        for sid in to_remove:
            logger.info("Pruning idle sandbox %s", sid)
            await self.destroy_sandbox(sid)

    async def _prune_loop(self) -> None:
        """Background loop that prunes idle sandboxes every 60s."""
        while True:
            try:
                await asyncio.sleep(60)
                await self._prune_idle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in prune loop")
