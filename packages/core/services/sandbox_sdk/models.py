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


@dataclass
class ContainerConfig:
    image: str = "sandbox-skill:latest"
    network: str = "bridge"
    dns: list[str] = field(default_factory=list)
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 256
    read_only_root: bool = True
    tmpfs: list[str] = field(default_factory=lambda: ["/tmp", "/var/tmp"])
    cap_drop: list[str] = field(default_factory=lambda: ["ALL"])
    container_prefix: str = "skill-sbx-"
    workdir: str = "/skill"
    install_timeout: int = 120
    exec_timeout: int = 60
    volumes: list[str] = field(default_factory=list)  # bind mounts: ["host:container:mode"]

    def to_dict(self) -> dict:
        d = {
            "image": self.image,
            "network": self.network,
            "dns": self.dns,
            "memory": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "read_only_root": self.read_only_root,
            "tmpfs": self.tmpfs,
            "cap_drop": self.cap_drop,
            "container_prefix": self.container_prefix,
            "workdir": self.workdir,
            "install_timeout": self.install_timeout,
            "exec_timeout": self.exec_timeout,
        }
        if self.volumes:
            d["volumes"] = self.volumes
        return d


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
    expires_at: Optional[float] = None


@dataclass
class CreateSandboxResult:
    sandbox_id: str
    container_name: str
    status: str
    skill: SkillManifest
    workdir: str
    env_blocked: list[str] = field(default_factory=list)


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int

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
