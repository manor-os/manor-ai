"""
Sandbox SDK — async client for the Sandbox Service API.

Usage (async):
    async with SandboxClient("http://sandbox-service:8000") as client:
        sandbox = await client.create_from_files("my-skill", files={...})
        result = await client.exec(sandbox.sandbox_id, "python run.py")
        print(result.stdout)
"""
from __future__ import annotations

import httpx
from typing import Optional

from .exceptions import (
    SandboxConnectionError,
    SandboxError,
    SandboxNotFoundError,
    SandboxRuntimeError,
    SandboxSecurityError,
)
from .models import (
    ContainerConfig,
    CreateSandboxResult,
    ExecResult,
    FileReadBase64Result,
    FileReadResult,
    FileWriteResult,
    LoadSkillResult,
    SandboxInfo,
    SkillContext,
    SkillManifest,
)


def _config_payload(config: Optional[ContainerConfig | dict]) -> dict | None:
    if not config:
        return None
    if isinstance(config, dict):
        return {k: v for k, v in config.items() if v is not None}
    return config.to_dict()

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 120.0


def _parse_skill(data: dict) -> SkillManifest:
    return SkillManifest(
        name=data["name"],
        skill_dir=data["skill_dir"],
        description=data.get("description", ""),
        scripts=data.get("scripts", []),
        requirements_txt=data.get("requirements_txt"),
        env_vars=data.get("env_vars", {}),
        entry_hint=data.get("entry_hint"),
        needs_sandbox=data.get("needs_sandbox", False),
    )


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text

    if resp.status_code == 404:
        raise SandboxNotFoundError(detail, status_code=404)
    if resp.status_code == 403:
        raise SandboxSecurityError(detail, status_code=403)
    if resp.status_code >= 500:
        raise SandboxRuntimeError(detail, status_code=resp.status_code)
    raise SandboxError(detail, status_code=resp.status_code)


class SandboxClient:
    """Async HTTP client for the Sandbox Service."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def __aenter__(self) -> "SandboxClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def health(self) -> dict:
        try:
            resp = await self._http.get("/health")
            _raise_for_status(resp)
            return resp.json()
        except httpx.ConnectError as exc:
            raise SandboxConnectionError(
                f"Cannot connect to Sandbox Service at {self._base_url}: {exc}"
            ) from exc

    async def create_from_files(
        self,
        skill_name: str,
        files: dict[str, str],
        env: dict[str, str] | None = None,
        allowed_sensitive_keys: list[str] | None = None,
        config: Optional[ContainerConfig | dict] = None,
        auto_install: bool = True,
    ) -> CreateSandboxResult:
        """Create a sandbox from in-memory file contents (MinIO / workspace skills)."""
        body: dict = {
            "skill_name": skill_name,
            "files": files,
            "env": env or {},
            "allowed_sensitive_keys": allowed_sensitive_keys or [],
            "auto_install": auto_install,
            "source": "workspace",
        }
        config_payload = _config_payload(config)
        if config_payload:
            body["config_overrides"] = config_payload
        resp = await self._http.post("/api/v1/sandbox/create-from-files", json=body)
        _raise_for_status(resp)
        data = resp.json()
        return CreateSandboxResult(
            sandbox_id=data["sandbox_id"],
            container_name=data["container_name"],
            status=data["status"],
            skill=_parse_skill(data["skill"]),
            workdir=data["workdir"],
            env_blocked=data.get("env_blocked", []),
        )

    async def create_from_builtin(
        self,
        skill_name: str,
        files: dict[str, str],
        env: dict[str, str] | None = None,
        allowed_sensitive_keys: list[str] | None = None,
        config: Optional[ContainerConfig | dict] = None,
        auto_install: bool = True,
    ) -> CreateSandboxResult:
        """Create a sandbox for a built-in (codebase) skill."""
        body: dict = {
            "skill_name": skill_name,
            "files": files,
            "env": env or {},
            "allowed_sensitive_keys": allowed_sensitive_keys or [],
            "auto_install": auto_install,
            "source": "builtin",
        }
        config_payload = _config_payload(config)
        if config_payload:
            body["config_overrides"] = config_payload
        resp = await self._http.post("/api/v1/sandbox/create-from-builtin", json=body)
        _raise_for_status(resp)
        data = resp.json()
        return CreateSandboxResult(
            sandbox_id=data["sandbox_id"],
            container_name=data["container_name"],
            status=data["status"],
            skill=_parse_skill(data["skill"]),
            workdir=data["workdir"],
            env_blocked=data.get("env_blocked", []),
        )

    async def load_skill(
        self,
        sandbox_id: str,
        skill_name: str,
        files: dict[str, str],
        auto_install: bool = True,
        idle_threshold: int = 30,
        source: str = "workspace",
    ) -> LoadSkillResult:
        """Load a new skill into an existing idle sandbox (reuse mode).

        Raises SandboxError(status_code=409) if the sandbox is busy.
        """
        body: dict = {
            "skill_name": skill_name,
            "files": files,
            "auto_install": auto_install,
            "idle_threshold": idle_threshold,
            "source": source,
        }
        resp = await self._http.post(f"/api/v1/sandbox/{sandbox_id}/load-skill", json=body)
        _raise_for_status(resp)
        data = resp.json()
        return LoadSkillResult(
            sandbox_id=data["sandbox_id"],
            skill=_parse_skill(data["skill"]),
            reused=data.get("reused", True),
        )

    async def list(self) -> list[SandboxInfo]:
        resp = await self._http.get("/api/v1/sandbox")
        _raise_for_status(resp)
        return [
            SandboxInfo(
                sandbox_id=d["sandbox_id"],
                container_name=d["container_name"],
                status=d["status"],
                skill_name=d["skill_name"],
                workdir=d["workdir"],
                created_at=d["created_at"],
                last_used_at=d["last_used_at"],
                config=d.get("config", {}),
                active_command=d.get("active_command"),
                expires_at=d.get("expires_at"),
            )
            for d in resp.json()
        ]

    async def status(self, sandbox_id: str) -> SandboxInfo:
        resp = await self._http.get(f"/api/v1/sandbox/{sandbox_id}")
        _raise_for_status(resp)
        d = resp.json()
        return SandboxInfo(
            sandbox_id=d["sandbox_id"],
            container_name=d["container_name"],
            status=d["status"],
            skill_name=d["skill_name"],
            workdir=d["workdir"],
            created_at=d["created_at"],
            last_used_at=d["last_used_at"],
            config=d.get("config", {}),
            active_command=d.get("active_command"),
            expires_at=d.get("expires_at"),
        )

    async def destroy(self, sandbox_id: str) -> None:
        resp = await self._http.delete(f"/api/v1/sandbox/{sandbox_id}")
        _raise_for_status(resp)

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: str | None = None,
    ) -> ExecResult:
        body: dict = {"command": command, "timeout": timeout}
        if workdir:
            body["workdir"] = workdir
        http_timeout = float(timeout) + 30.0
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/exec",
            json=body,
            timeout=http_timeout,
        )
        _raise_for_status(resp)
        d = resp.json()
        return ExecResult(stdout=d["stdout"], stderr=d["stderr"], exit_code=d["exit_code"])

    async def read_file(
        self,
        sandbox_id: str,
        path: str,
        max_size: int = 65536,
    ) -> FileReadResult:
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/files/read",
            json={"path": path, "max_size": max_size},
        )
        _raise_for_status(resp)
        d = resp.json()
        return FileReadResult(
            path=d["path"], content=d["content"],
            size=d["size"], truncated=d.get("truncated", False),
        )

    async def read_file_base64(
        self,
        sandbox_id: str,
        path: str,
        max_size: int = 50 * 1024 * 1024,
    ) -> FileReadBase64Result:
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/files/read-base64",
            json={"path": path, "max_size": max_size},
            timeout=120.0,
        )
        _raise_for_status(resp)
        d = resp.json()
        return FileReadBase64Result(
            path=d["path"], content_base64=d["content_base64"], size=d["size"],
        )

    async def write_file(
        self,
        sandbox_id: str,
        path: str,
        content: str,
        mkdir: bool = True,
    ) -> FileWriteResult:
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/files/write",
            json={"path": path, "content": content, "mkdir": mkdir},
        )
        _raise_for_status(resp)
        d = resp.json()
        return FileWriteResult(path=d["path"], written=d["written"])

    async def write_file_base64(
        self,
        sandbox_id: str,
        path: str,
        content_base64: str,
        mkdir: bool = True,
    ) -> FileWriteResult:
        """Write a binary file into the sandbox from base64-encoded content.

        Delivers generated binary assets (e.g. images) straight into the sandbox
        without a text/utf-8 round-trip that would corrupt them.
        """
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/files/write-base64",
            json={"path": path, "content_base64": content_base64, "mkdir": mkdir},
        )
        _raise_for_status(resp)
        d = resp.json()
        return FileWriteResult(path=d["path"], written=d["written"])

    async def get_context(
        self,
        sandbox_id: str,
        max_files: int = 5,
        max_file_size: int = 8000,
    ) -> SkillContext:
        resp = await self._http.get(
            f"/api/v1/sandbox/{sandbox_id}/context",
            params={"max_files": max_files, "max_file_size": max_file_size},
        )
        _raise_for_status(resp)
        d = resp.json()
        return SkillContext(
            sandbox_id=d["sandbox_id"],
            skill=_parse_skill(d["skill"]),
            file_contents=d.get("file_contents", {}),
            sandbox_info=d.get("sandbox_info", ""),
        )
