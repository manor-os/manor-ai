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
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 256
    read_only_root: bool = True
    tmpfs: list[str] = Field(default_factory=lambda: ["/tmp", "/var/tmp"])
    cap_drop: list[str] = Field(default_factory=lambda: ["ALL"])
    container_prefix: str = "skill-sbx-"
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
    expires_at: Optional[float] = None


# ── API Request / Response ──


class CreateSandboxRequest(BaseModel):
    """Create a sandbox for a skill directory."""
    skill_dir: str = Field(..., description="Path to the skill directory on the host")
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
    env_blocked: list[str] = Field(default_factory=list)


class ExecRequest(BaseModel):
    command: str = Field(..., description="Shell command to execute inside sandbox")
    timeout: int = Field(60, description="Timeout in seconds", ge=1, le=600)
    workdir: Optional[str] = Field(None, description="Working directory override")


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class FileReadRequest(BaseModel):
    path: str = Field(..., description="File path inside the container")
    max_size: int = Field(65536, description="Max bytes to read")


class FileReadResponse(BaseModel):
    path: str
    content: str
    size: int
    truncated: bool = False


class FileReadBase64Request(BaseModel):
    path: str = Field(..., description="File path inside the container")
    max_size: int = Field(50 * 1024 * 1024, description="Max original file size in bytes")


class FileReadBase64Response(BaseModel):
    path: str
    content_base64: str
    size: int


class FileWriteRequest(BaseModel):
    path: str = Field(..., description="File path inside the container")
    content: str
    mkdir: bool = Field(True, description="Create parent directories if needed")


class FileWriteBase64Request(BaseModel):
    path: str = Field(..., description="File path inside the container")
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
    files: dict[str, str] = Field(
        ...,
        description="File contents keyed by relative path, e.g. {'run.py': '...', 'requirements.txt': '...'}",
    )
    env: dict[str, str] = Field(default_factory=dict)
    allowed_sensitive_keys: list[str] = Field(default_factory=list)
    config_overrides: Optional[ContainerConfig] = None
    auto_install: bool = Field(True, description="Auto-install requirements.txt if present")
    source: str = Field("workspace", description="Origin of skill files: 'workspace' (MinIO) or 'builtin' (codebase)")


class CreateFromBuiltinRequest(BaseModel):
    """Create a sandbox for a builtin (codebase) skill."""
    skill_name: str = Field(..., description="Builtin skill ID (must match builtin_skills/{id}/ directory)")
    files: dict[str, str] = Field(
        ...,
        description="File contents read from the codebase builtin_skills directory",
    )
    env: dict[str, str] = Field(default_factory=dict)
    allowed_sensitive_keys: list[str] = Field(default_factory=list)
    config_overrides: Optional[ContainerConfig] = None
    auto_install: bool = Field(True, description="Auto-install requirements.txt if present")


class LoadSkillRequest(BaseModel):
    """Load a new skill into an existing idle sandbox (reuse mode)."""
    skill_name: str = Field(..., description="Name/ID of the new skill to load")
    files: dict[str, str] = Field(
        ...,
        description="File contents keyed by relative path",
    )
    auto_install: bool = Field(True, description="Re-run pip install if requirements.txt changed")
    idle_threshold: int = Field(30, description="Seconds of inactivity required before loading is allowed", ge=0)
    source: str = Field("workspace", description="Origin of skill files: 'workspace' or 'builtin'")


class LoadSkillResponse(BaseModel):
    sandbox_id: str
    skill: SkillManifest
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
