"""
Pydantic models for the Sandbox Service API.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field



# ── Enums ──


class SandboxStatus(str, Enum):
    CREATING = "creating"
    RUNNING = "running"
    INSTALLING = "installing"
    EXECUTING = "executing"
    READY = "ready"
    ERROR = "error"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"


class ExecutionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls) -> set["ExecutionStatus"]:
        return {cls.COMPLETED, cls.FAILED, cls.CANCELLED}


class ExecutionEventType(str, Enum):
    PROGRESS = "progress"
    WARNING = "warning"
    ERROR = "error"
    NEED_INPUT = "need_input"
    NEED_FILE = "need_file"
    NEED_TOOL = "need_tool"
    NEED_CREDENTIAL = "need_credential"
    RESULT = "result"


class WorkspaceAccess(str, Enum):
    NONE = "none"
    RO = "ro"
    RW = "rw"


# ── Skill Manifest ──


class SkillManifest(BaseModel):
    name: str
    skill_dir: str
    description: str = ""
    scripts: list[str] = Field(default_factory=list)
    requirements_txt: Optional[str] = None
    package_json: Optional[str] = None
    env_vars: dict[str, str] = Field(default_factory=dict)
    entry_hint: Optional[str] = None
    needs_sandbox: bool = False


# ── Sandbox Lifecycle ──


class ContainerConfig(BaseModel):
    image: str = "sandbox-skill:latest"
    network: str = "bridge"
    dns: list[str] = Field(
        default_factory=list,
        description="Optional DNS servers to inject when network != 'none'. Leave empty to use Docker defaults.",
    )
    memory: str = "1g"
    cpus: float = 1.0
    pids_limit: int = 512
    read_only_root: bool = True
    tmpfs: list[str] = Field(default_factory=lambda: ["/tmp", "/var/tmp"])
    workdir_tmpfs_size: str = Field(
        "256m",
        description="Size of the writable tmpfs at /skills, where loaded Skills live side by side.",
    )
    tmp_tmpfs_size: str = Field("64m", description="Size of each of the /tmp and /var/tmp tmpfs.")
    home_tmpfs_size: str = Field("64m", description="Size of the /home/sandbox tmpfs (caches, profiles).")
    workspace_tmpfs_size: str = Field(
        "256m",
        description=(
            "Size of the shared /workspace tmpfs (each conversation or task works in "
            "/workspace/c/<area>, its agents in .../agents/<agent>); empty disables the mount."
        ),
    )
    max_concurrent_executions: int = Field(
        4,
        ge=1,
        le=64,
        description=(
            "Commands that may run at once in this sandbox (all of a user's work shares it); "
            "further bounded by pids_limit/32 and memory/256MiB."
        ),
    )
    cap_drop: list[str] = Field(default_factory=lambda: ["ALL"])
    container_prefix: str = "skill-sbx-"
    service_scope: str = "1"
    workdir: str = "/skill"
    install_timeout: int = 120
    exec_timeout: int = 60
    volumes: list[str] = Field(
        default_factory=list,
        description="Optional Docker bind mounts, e.g. ['/host/path:/workspace:ro'].",
    )




class SandboxInfo(BaseModel):
    sandbox_id: str
    container_name: str
    status: SandboxStatus
    skill_name: str
    workdir: str
    created_at: float
    last_used_at: float
    config: ContainerConfig
    active_command: Optional[str] = None
    active_execution_id: Optional[str] = None
    # Every command running now (agents of one conversation share a sandbox).
    active_execution_ids: list[str] = Field(default_factory=list)
    max_concurrent_executions: Optional[int] = None
    # Commands waiting for a free slot, in dispatch order (fair across work areas).
    queued_execution_ids: list[str] = Field(default_factory=list)
    # Skills loaded side by side, each in /skills/<name>.
    skills: list[str] = Field(default_factory=list)
    expires_at: Optional[float] = None


# ── API Request / Response ──


class CreateSandboxRequest(BaseModel):
    """Create a sandbox for a skill directory."""
    skill_dir: str = Field(..., description="Path to the skill directory on the host")
    idempotency_key: Optional[str] = Field(None, max_length=128)
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables to inject")
    allowed_sensitive_keys: list[str] = Field(
        default_factory=list,
        description="Env var names that are allowed even if they match sensitive patterns (e.g. API keys the skill needs)",
    )
    config_overrides: Optional[ContainerConfig] = Field(
        None, description="Override default container config"
    )
    auto_install: bool = Field(True, description="Auto-install requirements.txt if present")


class CreateSandboxResponse(BaseModel):
    sandbox_id: str
    container_name: str
    status: SandboxStatus
    skill: SkillManifest
    workdir: str
    skill_dir: Optional[str] = None
    env_blocked: list[str] = Field(default_factory=list)
    env_injection: str = Field(
        "exec",
        description=(
            "How skill env reaches processes: 'exec' means it is handed to each "
            "docker exec and replaced by load_skill, never baked into the container"
        ),
    )
    config: Optional[ContainerConfig] = Field(
        None,
        description=(
            "The container's effective config after the runner's defaults, the "
            "caller's overrides and the host clamp were applied"
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Adjustments the runner made, e.g. a memory request clamped to this host",
    )


class ExecRequest(BaseModel):
    command: str = Field(..., description="Shell command to execute inside sandbox")
    timeout: int = Field(60, description="Timeout in seconds", ge=1, le=3600)
    workdir: Optional[str] = Field(None, description="Working directory override")
    execution_id: Optional[str] = Field(None, max_length=128)
    skill_env: bool = Field(
        True,
        description=(
            "Hand the loaded Skill's env to this command. False for an agent sharing the "
            "sandbox that did not invoke that Skill: it never receives the Skill's credentials."
        ),
    )
    skill: Optional[str] = Field(
        None,
        max_length=128,
        description=(
            "Which loaded Skill this command sees as /skill (its files live in /skills/<name>; "
            "cwd and SKILL_DIR default to it). Omitted: the most recently loaded Skill "
            "(single-Skill callers). Empty: no Skill view (an agent that did not invoke one)."
        ),
    )
    fair_key: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "Work area the command belongs to (a conversation or task). Commands waiting for a "
            "slot are dispatched round-robin across work areas, FIFO within one."
        ),
    )
    queue_timeout: float = Field(
        0,
        ge=0,
        le=3600,
        description=(
            "Seconds the command may wait for a free slot. 0 (default): answer sandbox_busy at "
            "once when every slot is taken."
        ),
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Env for this one command, layered over the skill env. Delivered like "
            "the skill env (a 0600 --env-file removed after the exec), never logged, "
            "never stored on the execution record and never kept for later commands."
        ),
    )
    side_channel: bool = Field(
        False,
        description=(
            "A short bookkeeping command of the platform's own (synchronous exec only): "
            "it runs next to the command slots like a file read or write -- never queued "
            "behind them, never with the Skill env or a per-exec env, at most "
            "SIDE_CHANNEL_MAX_TIMEOUT_SECONDS long."
        ),
    )


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    execution_id: Optional[str] = None
    # Why the runner killed the command, when it did: oom_killed, pids_killed,
    # disk_killed. None for a command that ended on its own (or by its timeout).
    outcome: Optional[str] = None
    outcome_detail: Optional[str] = None


class ExecutionStatusResponse(BaseModel):
    sandbox_id: str
    execution_id: str
    status: ExecutionStatus
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    # oom_killed / pids_killed / disk_killed / queue_timeout / sandbox_lost
    outcome: Optional[str] = None
    outcome_detail: Optional[str] = None
    # 1-based place in the sandbox's slot queue while QUEUED; None once started.
    queue_position: Optional[int] = None
    events: list["ExecutionEvent"] = Field(default_factory=list)
    next_sequence: int = 0
    waiting_for_response: bool = False


class ExecutionEvent(BaseModel):
    sequence: int = Field(..., ge=1)
    event_id: str = Field(..., min_length=1, max_length=128)
    type: ExecutionEventType
    message: str = Field("", max_length=2000)
    payload: dict = Field(default_factory=dict)
    requires_response: bool = False
    responded: bool = False
    created_at: float


class ExecutionResponseRequest(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    payload: dict = Field(default_factory=dict)
    message: str = Field("", max_length=2000)


class ExecutionResponseAck(BaseModel):
    sandbox_id: str
    execution_id: str
    event_id: str
    accepted: bool
    duplicate: bool = False


class CancelExecutionResponse(BaseModel):
    sandbox_id: str
    execution_id: str
    cancelled: bool


class FileReadRequest(BaseModel):
    path: str = Field(..., description="File path inside the container")
    skill: Optional[str] = Field(None, max_length=128, description="Skill view for /skill paths (see ExecRequest.skill)")
    max_size: int = Field(65536, description="Max bytes to read")


class FileReadResponse(BaseModel):
    path: str
    content: str
    size: int
    truncated: bool = False


class FileReadBase64Request(BaseModel):
    path: str = Field(..., description="File path inside the container")
    skill: Optional[str] = Field(None, max_length=128, description="Skill view for /skill paths (see ExecRequest.skill)")
    max_size: int = Field(50 * 1024 * 1024, description="Max original file size in bytes")


class FileReadBase64Response(BaseModel):
    path: str
    content_base64: str
    size: int


class FileTailRequest(BaseModel):
    path: str = Field(..., max_length=4096, description="Absolute path under an allowed tail prefix")
    offset: int = Field(0, ge=0, le=2**62)
    limit: int = Field(256 * 1024, ge=1, le=256 * 1024)


class FileTailResponse(BaseModel):
    path: str
    offset: int
    data_base64: str
    next_offset: int
    size: int
    eof: bool


class FileWriteRequest(BaseModel):
    path: str = Field(..., description="File path inside the container")
    skill: Optional[str] = Field(None, max_length=128, description="Skill view for /skill paths (see ExecRequest.skill)")
    content: str
    mkdir: bool = Field(True, description="Create parent directories if needed")


class FileWriteBase64Request(BaseModel):
    path: str = Field(..., description="File path inside the container")
    skill: Optional[str] = Field(None, max_length=128, description="Skill view for /skill paths (see ExecRequest.skill)")
    content_base64: str = Field(..., description="Base64-encoded binary file content")
    mkdir: bool = Field(True, description="Create parent directories if needed")


class FileWriteResponse(BaseModel):
    path: str
    written: bool


class SkillScanRequest(BaseModel):
    skill_dir: str


class CreateFromFilesRequest(BaseModel):
    """Create a sandbox by providing file contents directly (for MinIO / remote workspace skills)."""
    skill_name: str = Field(..., description="Skill identifier / name")
    idempotency_key: Optional[str] = Field(None, max_length=128)
    files: dict[str, str] = Field(
        ...,
        description="File contents keyed by relative path, e.g. {'run.py': '...', 'requirements.txt': '...'}",
    )
    env: dict[str, str] = Field(default_factory=dict)
    allowed_sensitive_keys: list[str] = Field(default_factory=list)
    config_overrides: Optional[ContainerConfig] = None
    auto_install: bool = Field(True, description="Auto-install requirements.txt if present")
    source: str = Field("workspace", description="Origin of skill files: 'workspace' (MinIO) or 'builtin' (codebase)")


class BakedSkillRef(BaseModel):
    """A built-in Skill the sandbox image holds, by its content digest.

    Sent instead of the files when the runner's ``/health`` advertises the
    same digest (``baked_skills``); the image's copy is materialized into the
    workdir. A runner without that exact digest answers 409
    ``baked_skill_unavailable`` and the caller sends the files.
    """
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class CreateFromBuiltinRequest(BaseModel):
    """Create a sandbox for a builtin (codebase) skill."""
    skill_name: str = Field(..., description="Builtin skill ID (must match builtin_skills/{id}/ directory)")
    idempotency_key: Optional[str] = Field(None, max_length=128)
    files: dict[str, str] = Field(
        default_factory=dict,
        description="File contents read from the codebase builtin_skills directory (empty with baked_skill)",
    )
    baked_skill: Optional[BakedSkillRef] = Field(
        None,
        description="Use the image's baked copy of this built-in Skill instead of files",
    )
    env: dict[str, str] = Field(default_factory=dict)
    allowed_sensitive_keys: list[str] = Field(default_factory=list)
    config_overrides: Optional[ContainerConfig] = None
    auto_install: bool = Field(True, description="Auto-install requirements.txt if present")


class LoadSkillRequest(BaseModel):
    """Load a new skill into an existing idle sandbox (reuse mode)."""
    skill_name: str = Field(..., description="Name/ID of the new skill to load")
    files: dict[str, str] = Field(
        default_factory=dict,
        description="File contents keyed by relative path (empty with baked_skill)",
    )
    baked_skill: Optional[BakedSkillRef] = Field(
        None,
        description="Load the image's baked copy of this built-in Skill instead of files",
    )
    auto_install: bool = Field(True, description="Re-run pip install if requirements.txt changed")
    idle_threshold: int = Field(30, description="Seconds of inactivity required before loading is allowed", ge=0)
    source: str = Field("workspace", description="Origin of skill files: 'workspace' or 'builtin'")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Skill env for later exec calls; replaces the previous skill's env entirely",
    )
    allowed_sensitive_keys: list[str] = Field(
        default_factory=list,
        description="Sensitive env keys the caller explicitly allows (same policy as create)",
    )


class LoadSkillResponse(BaseModel):
    sandbox_id: str
    skill: SkillManifest
    # Where the Skill's files live; the Skill's commands see it as /skill.
    skill_dir: Optional[str] = None
    env_blocked: list[str] = Field(default_factory=list)
    reused: bool = True


class SkillRunRequest(BaseModel):
    """Full end-to-end skill execution: create sandbox → run → return results."""
    skill_dir: str
    env: dict[str, str] = Field(default_factory=dict)
    allowed_sensitive_keys: list[str] = Field(default_factory=list)
    commands: list[str] = Field(
        default_factory=list,
        description="Commands to execute. If empty, auto-detect entry point.",
    )
    auto_destroy: bool = Field(True, description="Destroy sandbox after execution")
    config_overrides: Optional[ContainerConfig] = None


class SkillRunStepResult(BaseModel):
    command: str
    stdout: str
    stderr: str
    exit_code: int


class SkillRunResponse(BaseModel):
    sandbox_id: str
    skill: SkillManifest
    success: bool
    steps: list[SkillRunStepResult]
    destroyed: bool = False


class SkillContextResponse(BaseModel):
    """Structured context for the LLM to understand a sandbox's skill."""
    sandbox_id: str
    skill: SkillManifest
    file_contents: dict[str, str] = Field(
        default_factory=dict,
        description="Key script file contents for LLM context",
    )
    sandbox_info: str = Field("", description="Human-readable sandbox constraint summary")
