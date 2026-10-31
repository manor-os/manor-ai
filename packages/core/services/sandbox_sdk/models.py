"""
Sandbox SDK data models.

Lightweight dataclasses that mirror the server-side Pydantic models.
No pydantic dependency required on the client side.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SkillManifest:
    name: str
    skill_dir: str
    description: str = ""
    scripts: list[str] = field(default_factory=list)
    requirements_txt: Optional[str] = None
    env_vars: dict[str, str] = field(default_factory=dict)
    entry_hint: Optional[str] = None
    needs_sandbox: bool = False


#: The container settings a caller may override; the runner owns every other
#: field (image, security posture, mounts, naming, workdir) and answers 400
#: ``config_override_rejected`` when one is sent.
#: Container settings only the runner's own config decides (``SANDBOX_IMAGE``,
#: ``SANDBOX_WORKDIR``, its security posture and mounts). A create that names
#: one is refused before it is sent; the runner refuses it too (400
#: ``config_override_rejected``).
RUNNER_OWNED_CONTAINER_FIELDS = (
    "image",
    "workdir",
    "read_only_root",
    "tmpfs",
    "volumes",
    "cap_drop",
    "dns",
    "container_prefix",
    "service_scope",
)

OVERRIDABLE_CONTAINER_FIELDS = (
    "memory",
    "cpus",
    "pids_limit",
    "workdir_tmpfs_size",
    "network",
    "exec_timeout",
    "install_timeout",
)


@dataclass
class ContainerConfig:
    """Overrides for a sandbox's container; unset (None) fields keep the runner default.

    Each runner bounds every field (``SANDBOX_OVERRIDE_MAX_*``) and clamps
    memory/cpus to what its host can give, reporting that in the create
    response's ``warnings``.
    """

    memory: Optional[str] = None
    cpus: Optional[float] = None
    pids_limit: Optional[int] = None
    workdir_tmpfs_size: Optional[str] = None
    network: Optional[str] = None
    exec_timeout: Optional[int] = None
    install_timeout: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            name: getattr(self, name)
            for name in OVERRIDABLE_CONTAINER_FIELDS
            if getattr(self, name) is not None
        }


@dataclass
class SandboxInfo:
    sandbox_id: str
    container_name: str
    status: str
    skill_name: str
    workdir: str
    created_at: float
    last_used_at: float
    config: dict = field(default_factory=dict)
    active_command: Optional[str] = None
    active_execution_id: Optional[str] = None
    expires_at: Optional[float] = None
    # Every command running now: agents of one conversation share a sandbox.
    active_execution_ids: list[str] = field(default_factory=list)
    max_concurrent_executions: Optional[int] = None
    # Commands waiting for a slot (fair FIFO) and the Skills loaded side by side.
    queued_execution_ids: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


@dataclass
class CreateSandboxResult:
    sandbox_id: str
    container_name: str
    status: str
    skill: SkillManifest
    workdir: str
    env_blocked: list[str] = field(default_factory=list)
    # "exec": skill env is handed to each docker exec and replaced by
    # load_skill; "create": an older service baked it into the container.
    env_injection: str = "create"
    # The container's effective config (runner defaults + overrides + host
    # clamp); empty from older runners.
    config: dict = field(default_factory=dict)
    # Adjustments the runner made, e.g. memory clamped to this host.
    warnings: list[str] = field(default_factory=list)
    # Where the Skill's files live (/skills/<name>) on a runner that keeps
    # Skills side by side; None on an older runner with one /skill.
    skill_dir: Optional[str] = None


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    execution_id: Optional[str] = None
    # oom_killed / pids_killed / disk_killed when the runner killed it.
    outcome: Optional[str] = None
    outcome_detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Combined stdout + stderr for convenience."""
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts)


@dataclass
class CancelExecutionResult:
    sandbox_id: str
    execution_id: str
    cancelled: bool


@dataclass
class ExecutionEventResult:
    sequence: int
    event_id: str
    type: str
    message: str = ""
    payload: dict = field(default_factory=dict)
    requires_response: bool = False
    responded: bool = False
    created_at: float = 0.0


@dataclass
class ExecutionStatusResult:
    sandbox_id: str
    execution_id: str
    status: str
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    events: list[ExecutionEventResult] = field(default_factory=list)
    next_sequence: int = 0
    waiting_for_response: bool = False
    # oom_killed / pids_killed / disk_killed / queue_timeout / sandbox_lost
    outcome: Optional[str] = None
    outcome_detail: Optional[str] = None
    # Place in the sandbox's slot queue while "queued".
    queue_position: Optional[int] = None

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}


@dataclass
class ExecutionResponseResult:
    sandbox_id: str
    execution_id: str
    event_id: str
    accepted: bool
    duplicate: bool = False


@dataclass
class FileReadResult:
    path: str
    content: str
    size: int
    truncated: bool = False


@dataclass
class FileReadBase64Result:
    path: str
    content_base64: str
    size: int


@dataclass
class TailFileResult:
    path: str
    data: bytes
    next_offset: int
    size: int
    eof: bool


@dataclass
class FileWriteResult:
    path: str
    written: bool


@dataclass
class SkillContext:
    """Structured context for an LLM to understand the sandboxed skill."""
    sandbox_id: str
    skill: SkillManifest
    file_contents: dict[str, str] = field(default_factory=dict)
    sandbox_info: str = ""

    def format_for_llm(self) -> str:
        """Format the context as a human-readable prompt block for an LLM."""
        sections: list[str] = []
        sections.append(f"## Skill: {self.skill.name}\n")
        if self.skill.description:
            desc = self.skill.description
            if len(desc) > 2000:
                desc = desc[:2000] + "\n... (truncated)"
            sections.append(f"### Description\n{desc}\n")
        if self.skill.scripts:
            sections.append("### Available Scripts")
            for s in self.skill.scripts:
                sections.append(f"- `{s}`")
            sections.append("")
        if self.skill.entry_hint:
            sections.append(f"### Suggested Entry Point\n`{self.skill.entry_hint}`\n")
        if self.skill.requirements_txt:
            sections.append("### Dependencies\nInstalled from `requirements.txt`.\n")
        env_keys = list(self.skill.env_vars.keys())
        if env_keys:
            sections.append("### Environment Variables")
            for key in env_keys:
                sections.append(f"- `{key}`")
            sections.append("")
        if self.file_contents:
            sections.append("### Key File Contents")
            for path, content in self.file_contents.items():
                sections.append(f"\n#### `{path}`\n```\n{content}\n```")
            sections.append("")
        if self.sandbox_info:
            sections.append(f"### Sandbox Constraints\n{self.sandbox_info}")
        return "\n".join(sections)


@dataclass
class SkillRunStepResult:
    command: str
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class SkillRunResult:
    sandbox_id: str
    skill: SkillManifest
    success: bool
    steps: list[SkillRunStepResult]
    destroyed: bool = False


@dataclass
class LoadSkillResult:
    """Result of loading a new skill into an existing sandbox."""
    sandbox_id: str
    skill: SkillManifest
    reused: bool = True
    env_blocked: list[str] = field(default_factory=list)
    # Where the Skill's files live on a runner that keeps Skills side by side.
    skill_dir: Optional[str] = None
