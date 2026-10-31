"""
Sandbox SDK — async client for the Sandbox Service API.

Usage (async):
    async with SandboxClient("http://sandbox-service:8000") as client:
        sandbox = await client.create_from_files("my-skill", files={...})
        result = await client.exec(sandbox.sandbox_id, "python run.py")
        print(result.stdout)
"""
from __future__ import annotations

import base64

import httpx
from typing import Awaitable, Callable, Optional


from .capabilities import (
    REQUEST_GATES,
    ROUTE_GATES,
    UNKNOWN,
    RunnerCapabilities,
    describe,
    gate_request,
    outdated_message,
)
from .exceptions import (
    SandboxCapacityError,
    SandboxConnectionError,
    SandboxError,
    SandboxNotFoundError,
    SandboxRequestError,
    SandboxRunnerOutdatedError,
    SandboxRuntimeError,
    SandboxSecurityError,
    SandboxTimeoutError,
)
from .models import (
    RUNNER_OWNED_CONTAINER_FIELDS,
    CancelExecutionResult,
    ContainerConfig,
    CreateSandboxResult,
    ExecResult,
    ExecutionEventResult,
    ExecutionResponseResult,
    ExecutionStatusResult,
    FileReadBase64Result,
    FileReadResult,
    FileWriteResult,
    TailFileResult,
    LoadSkillResult,
    SandboxInfo,
    SkillContext,
    SkillManifest,
)




def _config_payload(config: Optional[ContainerConfig | dict]) -> dict | None:
    if not config:
        return None
    if isinstance(config, dict):
        owned = sorted(name for name in config if name in RUNNER_OWNED_CONTAINER_FIELDS)
        if owned:
            raise ValueError(
                "Container setting(s) " + ", ".join(owned) + " are the runner's own config "
                "(SANDBOX_IMAGE, SANDBOX_WORKDIR, ...) and cannot be requested by a caller; "
                "route the work to a runner pool configured for it instead."
            )
        return {k: v for k, v in config.items() if v is not None}
    return config.to_dict()

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 120.0
DEFAULT_PROVISION_TIMEOUT = 360.0


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
    body: dict = {}
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            body = parsed
        detail = body.get("detail", resp.text) if body else resp.text
    except Exception:
        detail = resp.text
    if not isinstance(detail, str):
        detail = str(detail)
    code = body.get("code") if isinstance(body.get("code"), str) else None

    status = resp.status_code
    kwargs = {"status_code": status, "code": code, "body": body}
    if status == 404:
        raise SandboxNotFoundError(detail, **kwargs)
    if status == 403:
        raise SandboxSecurityError(detail, **kwargs)
    if status == 429:
        raise SandboxCapacityError(detail, **kwargs)
    if status in (400, 413, 422):
        raise SandboxRequestError(detail, **kwargs)
    if status == 504:
        raise SandboxTimeoutError(detail, **kwargs)
    if status >= 500:
        raise SandboxRuntimeError(detail, **kwargs)
    raise SandboxError(detail, **kwargs)


def _skill_field(skill: str | None) -> dict:
    """The Skill view of a file operation (omitted: the runner's most recent Skill)."""
    return {} if skill is None else {"skill": skill}


def _exec_body(
    command: str,
    timeout: int,
    workdir: str | None,
    execution_id: str | None,
    skill_env: bool | None,
    skill: str | None,
    fair_key: str | None,
    queue_timeout: float | None,
) -> dict:
    body: dict = {"command": command, "timeout": timeout}
    if workdir:
        body["workdir"] = workdir
    if execution_id is not None:
        body["execution_id"] = execution_id
    if skill_env is not None:
        # False: run without the Skill's env (an agent sharing the sandbox
        # that did not invoke that Skill).
        body["skill_env"] = bool(skill_env)
    if skill is not None:
        body["skill"] = skill
    if fair_key:
        body["fair_key"] = str(fair_key)[:255]
    if queue_timeout:
        body["queue_timeout"] = float(queue_timeout)
    return body


def _parse_sandbox_info(d: dict) -> SandboxInfo:
    """Parse the shared status/list/touch response shape."""

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
        active_execution_id=d.get("active_execution_id"),
        expires_at=d.get("expires_at"),
        active_execution_ids=list(d.get("active_execution_ids") or []),
        max_concurrent_executions=d.get("max_concurrent_executions"),
        queued_execution_ids=list(d.get("queued_execution_ids") or []),
        skills=list(d.get("skills") or []),
    )


class SandboxClient:
    """Async HTTP client for the Sandbox Service."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        api_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        headers: dict[str, str] = {}
        token = (api_token or "").strip()
        if token:
            headers["X-Manor-Sandbox-Token"] = token
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers=headers,
            transport=transport,
        )
        # What the runner can do (``sandbox_sdk.capabilities``): set directly,
        # or read on the first gated request through ``capability_loader``
        # (the control plane's cached /health read per runner). Neither set:
        # requests go out unchanged.
        self.capabilities: RunnerCapabilities | None = None
        self.capability_loader: Callable[[], Awaitable[RunnerCapabilities]] | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    async def runner_capabilities(self) -> RunnerCapabilities:
        if self.capabilities is None and self.capability_loader is not None:
            try:
                self.capabilities = await self.capability_loader()
            except Exception:  # noqa: BLE001 - unknown, never "outdated"
                return UNKNOWN
        return self.capabilities or UNKNOWN

    async def _gate(self, method: str, route: str, body: dict | None = None) -> dict | None:
        """The body this runner may receive; refuses what it would silently get wrong."""

        if (method, route) not in REQUEST_GATES and (method, route) not in ROUTE_GATES:
            return body
        capabilities = await self.runner_capabilities()
        sent, missing = gate_request(capabilities, method, route, body)
        if missing and not capabilities.reported:
            needs = ", ".join(describe(name) for name in missing)
            raise SandboxRunnerOutdatedError(
                f"The sandbox runner's capabilities could not be read, and this needs proof that "
                f"it supports {needs}. Nothing was run; retry once the runner answers /health.",
                missing=missing,
                reported=False,
            )
        if missing:
            raise SandboxRunnerOutdatedError(
                outdated_message(missing, build_version=capabilities.build_version),
                missing=missing,
                build_version=capabilities.build_version,
            )
        return sent

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

    def _provision_timeout(self) -> httpx.Timeout:
        # Creating/loading a skill includes Docker setup and dependency installs.
        # Keep normal reads short while allowing that bounded provisioning work.
        timeout = self._http.timeout
        read = timeout.read
        if read is not None:
            read = max(read, DEFAULT_PROVISION_TIMEOUT)
        return httpx.Timeout(connect=timeout.connect, read=read, write=timeout.write, pool=timeout.pool)

    async def create_from_files(
        self,
        skill_name: str,
        files: dict[str, str],
        env: dict[str, str] | None = None,
        allowed_sensitive_keys: list[str] | None = None,
        config: Optional[ContainerConfig | dict] = None,
        auto_install: bool = True,
        idempotency_key: str | None = None,
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
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        body = await self._gate("POST", "/api/v1/sandbox/create-from-files", body)
        resp = await self._http.post(
            "/api/v1/sandbox/create-from-files", json=body, timeout=self._provision_timeout()
        )
        _raise_for_status(resp)
        data = resp.json()
        return CreateSandboxResult(
            sandbox_id=data["sandbox_id"],
            container_name=data["container_name"],
            status=data["status"],
            skill=_parse_skill(data["skill"]),
            workdir=data["workdir"],
            env_blocked=data.get("env_blocked", []),
            env_injection=str(data.get("env_injection") or "create"),
            config=dict(data.get("config") or {}),
            warnings=[str(item) for item in data.get("warnings") or []],
            skill_dir=data.get("skill_dir"),
        )

    async def create_from_builtin(
        self,
        skill_name: str,
        files: dict[str, str],
        env: dict[str, str] | None = None,
        allowed_sensitive_keys: list[str] | None = None,
        config: Optional[ContainerConfig | dict] = None,
        auto_install: bool = True,
        idempotency_key: str | None = None,
        baked_skill: dict[str, str] | None = None,
    ) -> CreateSandboxResult:
        """Create a sandbox for a built-in (codebase) skill.

        ``baked_skill={"name", "digest"}`` asks the runner to use its image's
        copy instead of ``files``; only send it when the runner's
        ``health()["baked_skills"]`` holds that exact digest (see
        ``packages.core.services.sandbox_baked_skills``, which also falls back
        to the files on ``baked_skill_unavailable``).
        """
        body: dict = {
            "skill_name": skill_name,
            "files": files,
            "env": env or {},
            "allowed_sensitive_keys": allowed_sensitive_keys or [],
            "auto_install": auto_install,
            "source": "builtin",
        }
        if baked_skill is not None:
            body["baked_skill"] = {"name": baked_skill["name"], "digest": baked_skill["digest"]}
        config_payload = _config_payload(config)
        if config_payload:
            body["config_overrides"] = config_payload
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        body = await self._gate("POST", "/api/v1/sandbox/create-from-builtin", body)
        resp = await self._http.post(
            "/api/v1/sandbox/create-from-builtin", json=body, timeout=self._provision_timeout()
        )
        _raise_for_status(resp)
        data = resp.json()
        return CreateSandboxResult(
            sandbox_id=data["sandbox_id"],
            container_name=data["container_name"],
            status=data["status"],
            skill=_parse_skill(data["skill"]),
            workdir=data["workdir"],
            env_blocked=data.get("env_blocked", []),
            env_injection=str(data.get("env_injection") or "create"),
            config=dict(data.get("config") or {}),
            warnings=[str(item) for item in data.get("warnings") or []],
            skill_dir=data.get("skill_dir"),
        )

    async def load_skill(
        self,
        sandbox_id: str,
        skill_name: str,
        files: dict[str, str],
        auto_install: bool = True,
        idle_threshold: int = 30,
        source: str = "workspace",
        env: dict[str, str] | None = None,
        allowed_sensitive_keys: list[str] | None = None,
        baked_skill: dict[str, str] | None = None,
    ) -> LoadSkillResult:
        """Load a new skill into an existing idle sandbox (reuse mode).

        ``env`` replaces the skill env later exec calls receive (an empty
        mapping clears the previous skill's credentials).
        ``baked_skill`` loads the image's copy of a built-in Skill instead of
        ``files`` (same contract as ``create_from_builtin``).
        Raises SandboxError(status_code=409) if the sandbox is busy.
        """
        body: dict = {
            "skill_name": skill_name,
            "files": files,
            "auto_install": auto_install,
            "idle_threshold": idle_threshold,
            "source": source,
            "env": env or {},
            "allowed_sensitive_keys": list(allowed_sensitive_keys or []),
        }
        if baked_skill is not None:
            body["baked_skill"] = {"name": baked_skill["name"], "digest": baked_skill["digest"]}
        body = await self._gate("POST", "/api/v1/sandbox/{sandbox_id}/load-skill", body)
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/load-skill", json=body, timeout=self._provision_timeout()
        )
        _raise_for_status(resp)
        data = resp.json()
        return LoadSkillResult(
            sandbox_id=data["sandbox_id"],
            skill=_parse_skill(data["skill"]),
            reused=data.get("reused", True),
            env_blocked=list(data.get("env_blocked") or []),
            skill_dir=data.get("skill_dir"),
        )

    async def list(self) -> list[SandboxInfo]:
        resp = await self._http.get("/api/v1/sandbox")
        _raise_for_status(resp)
        return [
            _parse_sandbox_info(d)
            for d in resp.json()
        ]

    async def status(self, sandbox_id: str) -> SandboxInfo:
        resp = await self._http.get(f"/api/v1/sandbox/{sandbox_id}")
        _raise_for_status(resp)
        return _parse_sandbox_info(resp.json())

    async def touch(self, sandbox_id: str) -> SandboxInfo:
        """Refresh a sandbox idle lease without executing a command."""

        resp = await self._http.post(f"/api/v1/sandbox/{sandbox_id}/touch")
        _raise_for_status(resp)
        return _parse_sandbox_info(resp.json())

    async def destroy(self, sandbox_id: str) -> None:
        resp = await self._http.delete(f"/api/v1/sandbox/{sandbox_id}")
        _raise_for_status(resp)

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: str | None = None,
        execution_id: str | None = None,
        skill_env: bool | None = None,
        skill: str | None = None,
        fair_key: str | None = None,
        queue_timeout: float | None = None,
        env: dict[str, str] | None = None,
        side_channel: bool = False,
    ) -> ExecResult:
        """Run a command and wait for it.

        ``queue_timeout``: seconds it may wait in the sandbox's fair FIFO for
        a free slot (a typed ``sandbox_queue_timeout`` 409 when none freed;
        it never started). ``skill``: the loaded Skill it sees as /skill
        ("" for none). ``fair_key``: its work area, for fair dispatch.
        ``env`` reaches this one command only (layered over the skill env);
        it is how short-lived per-command credentials travel. Never logged.
        ``side_channel``: a short bookkeeping command of the platform's own
        (bash's working-copy scripts) that runs next to the command slots,
        like a file read or write -- it never waits in the slot queue, takes
        no Skill env and no per-exec env.
        """
        body = _exec_body(
            command, timeout, workdir, execution_id, skill_env, skill, fair_key, queue_timeout,
        )
        if env:
            body["env"] = dict(env)
        if side_channel:
            body["side_channel"] = True
        body = await self._gate("POST", "/api/v1/sandbox/{sandbox_id}/exec", body)
        queue_timeout = body.get("queue_timeout")
        http_timeout = float(timeout) + float(queue_timeout or 0) + 30.0
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/exec",
            json=body,
            timeout=http_timeout,
        )
        _raise_for_status(resp)
        d = resp.json()
        return ExecResult(
            stdout=d["stdout"],
            stderr=d["stderr"],
            exit_code=d["exit_code"],
            execution_id=d.get("execution_id"),
            outcome=d.get("outcome"),
            outcome_detail=d.get("outcome_detail"),
        )

    async def cancel_execution(
        self,
        sandbox_id: str,
        execution_id: str,
    ) -> CancelExecutionResult:
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/executions/{execution_id}/cancel"
        )
        _raise_for_status(resp)
        data = resp.json()
        return CancelExecutionResult(
            sandbox_id=data["sandbox_id"],
            execution_id=data["execution_id"],
            cancelled=bool(data["cancelled"]),
        )

    @staticmethod
    def _parse_execution_status(data: dict) -> ExecutionStatusResult:
        events = [
            ExecutionEventResult(
                sequence=int(event["sequence"]),
                event_id=event["event_id"],
                type=event["type"],
                message=event.get("message", ""),
                payload=dict(event.get("payload") or {}),
                requires_response=bool(event.get("requires_response", False)),
                responded=bool(event.get("responded", False)),
                created_at=float(event.get("created_at") or 0.0),
            )
            for event in data.get("events", [])
        ]
        return ExecutionStatusResult(
            sandbox_id=data["sandbox_id"],
            execution_id=data["execution_id"],
            status=data["status"],
            created_at=data["created_at"],
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            stdout=data.get("stdout"),
            stderr=data.get("stderr"),
            exit_code=data.get("exit_code"),
            error=data.get("error"),
            events=events,
            next_sequence=int(data.get("next_sequence") or 0),
            waiting_for_response=bool(data.get("waiting_for_response", False)),
            outcome=data.get("outcome"),
            outcome_detail=data.get("outcome_detail"),
            queue_position=data.get("queue_position"),
        )

    async def start_exec(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: str | None = None,
        execution_id: str | None = None,
        skill_env: bool | None = None,
        skill: str | None = None,
        fair_key: str | None = None,
        queue_timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecutionStatusResult:
        """Start a command without waiting for it (see ``exec`` for the options).

        With ``queue_timeout`` a full sandbox answers ``queued`` (with its
        ``queue_position``) instead of busy. ``env`` reaches this one command
        only and lives no longer than it (the runner never stores it); the
        caller collects the command and revokes any credential in it.
        """
        body = _exec_body(
            command, timeout, workdir, execution_id, skill_env, skill, fair_key, queue_timeout,
        )
        if env:
            body["env"] = dict(env)
        body = await self._gate("POST", "/api/v1/sandbox/{sandbox_id}/exec/start", body)
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/exec/start",
            json=body,
        )
        _raise_for_status(resp)
        return self._parse_execution_status(resp.json())

    async def execution_status(
        self,
        sandbox_id: str,
        execution_id: str,
        after_sequence: int = 0,
        wait_seconds: float = 0,
    ) -> ExecutionStatusResult:
        """An execution's state; ``wait_seconds`` (<= 60) long-polls until it ends."""
        params: dict | None = {"after_sequence": max(0, int(after_sequence))} if after_sequence else None
        timeout = None
        if wait_seconds and wait_seconds > 0:
            wait = min(60.0, float(wait_seconds))
            params = {**(params or {}), "wait_seconds": wait}
            timeout = wait + 30.0
        await self._gate("GET", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}")
        resp = await self._http.get(
            f"/api/v1/sandbox/{sandbox_id}/executions/{execution_id}",
            params=params,
            **({"timeout": timeout} if timeout else {}),
        )
        _raise_for_status(resp)
        return self._parse_execution_status(resp.json())

    async def send_execution_response(
        self,
        sandbox_id: str,
        execution_id: str,
        event_id: str,
        *,
        payload: dict | None = None,
        message: str = "",
    ) -> ExecutionResponseResult:
        await self._gate("POST", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}/responses")
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/executions/{execution_id}/responses",
            json={
                "event_id": event_id,
                "payload": dict(payload or {}),
                "message": message,
            },
        )
        _raise_for_status(resp)
        data = resp.json()
        return ExecutionResponseResult(
            sandbox_id=data["sandbox_id"],
            execution_id=data["execution_id"],
            event_id=data["event_id"],
            accepted=bool(data["accepted"]),
            duplicate=bool(data.get("duplicate", False)),
        )

    async def read_file(
        self,
        sandbox_id: str,
        path: str,
        max_size: int = 65536,
        skill: str | None = None,
    ) -> FileReadResult:
        body = await self._gate(
            "POST", "/api/v1/sandbox/{sandbox_id}/files/read",
            {"path": path, "max_size": max_size, **_skill_field(skill)},
        )
        resp = await self._http.post(f"/api/v1/sandbox/{sandbox_id}/files/read", json=body)
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
        skill: str | None = None,
    ) -> FileReadBase64Result:
        body = await self._gate(
            "POST", "/api/v1/sandbox/{sandbox_id}/files/read-base64",
            {"path": path, "max_size": max_size, **_skill_field(skill)},
        )
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/files/read-base64", json=body, timeout=120.0,
        )
        _raise_for_status(resp)
        d = resp.json()
        return FileReadBase64Result(
            path=d["path"], content_base64=d["content_base64"], size=d["size"],
        )

    async def tail_file(
        self,
        sandbox_id: str,
        path: str,
        offset: int = 0,
        limit: int = 256 * 1024,
    ) -> TailFileResult:
        await self._gate("POST", "/api/v1/sandbox/{sandbox_id}/files/tail")
        resp = await self._http.post(
            f"/api/v1/sandbox/{sandbox_id}/files/tail",
            json={"path": path, "offset": max(0, int(offset)), "limit": int(limit)},
            timeout=45.0,
        )
        _raise_for_status(resp)
        d = resp.json()
        return TailFileResult(
            path=d["path"], data=base64.b64decode(d["data_base64"]),
            next_offset=int(d["next_offset"]), size=int(d["size"]), eof=bool(d["eof"]),
        )

    async def write_file(
        self,
        sandbox_id: str,
        path: str,
        content: str,
        mkdir: bool = True,
        skill: str | None = None,
    ) -> FileWriteResult:
        body = await self._gate(
            "POST", "/api/v1/sandbox/{sandbox_id}/files/write",
            {"path": path, "content": content, "mkdir": mkdir, **_skill_field(skill)},
        )
        resp = await self._http.post(f"/api/v1/sandbox/{sandbox_id}/files/write", json=body)
        _raise_for_status(resp)
        d = resp.json()
        return FileWriteResult(path=d["path"], written=d["written"])

    async def write_file_base64(
        self,
        sandbox_id: str,
        path: str,
        content_base64: str,
        mkdir: bool = True,
        skill: str | None = None,
    ) -> FileWriteResult:
        """Write a binary file into the sandbox from base64-encoded content.

        Delivers generated binary assets (e.g. images) straight into the sandbox
        without a text/utf-8 round-trip that would corrupt them.
        """
        body = await self._gate(
            "POST", "/api/v1/sandbox/{sandbox_id}/files/write-base64",
            {"path": path, "content_base64": content_base64, "mkdir": mkdir, **_skill_field(skill)},
        )
        resp = await self._http.post(f"/api/v1/sandbox/{sandbox_id}/files/write-base64", json=body)
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
